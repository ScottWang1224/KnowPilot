"""Chat API, conversation storage, cost guards, feedback and admin APIs (phase 4).

Only the fake provider is used: no network, no API key. One seeded database serves the
module; every test makes its own users, so tests do not see each other's conversations.
"""
import itertools
import json
import sqlite3
import threading
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

_ids = itertools.count(1)


@pytest.fixture(scope='module')
def chat_app(tmp_path_factory):
    from app import create_app
    from core.config import Settings
    from core.database import Database
    from scripts.migrate import migrate
    from scripts.seed import seed
    db = Database.sqlite(tmp_path_factory.mktemp('chat') / 'chat.db')
    migrate(db, 'sqlite')
    seed(db)
    departments = {r['code']: r['id'] for r in db.query('SELECT id,code FROM departments')}
    settings = Settings.from_env()
    with TestClient(create_app(settings, db), base_url='http://testserver') as client:
        yield client, db, settings, departments


def new_user(chat_app, department='MFG', role='user'):
    """A fresh user and their bearer headers."""
    from core.security import create_token
    from repositories import users
    _, db, settings, departments = chat_app
    n = next(_ids)
    user_id = users.create(db, email=f'chat{n}@example.com', name=f'測試{n}', password_hash='x',
                           department_id=departments[department], role=role)
    return user_id, {'Authorization': 'Bearer ' + create_token(user_id, settings)}


class Fakes:
    """app.state.chat_clients replacement: scripted router and answer providers shared by every request."""

    def __init__(self, router=(), answer=(), gate=None):
        from llm.providers.fake import FakeProvider
        self.router, self.answer = FakeProvider(router), FakeProvider(answer)
        self.gate = gate

    def __call__(self, budget, recorder):
        from llm.client import LLM
        from llm.guard import ConcurrencyGate
        from llm.pricing import Pricing
        return tuple(LLM(provider, spec, purpose=purpose, pricing=Pricing({}), budget=budget,
                         gate=self.gate or ConcurrencyGate(limit=5), recorder=recorder)
                     for provider, spec, purpose in ((self.router, 'fake:router', 'router'),
                                                     (self.answer, 'fake:answer', 'answer')))


@pytest.fixture
def fakes(chat_app):
    client = chat_app[0]

    def install(router=(), answer=(), gate=None):
        fake = Fakes(router, answer, gate)
        client.app.state.chat_clients = fake
        return fake
    return install


def first_chunk(db, code):
    from rag import index
    return next(c for c in index.get_index(db).chunks if c.doc_code == code)


def route_answer(chunk, state=None):
    return json.dumps({'action': 'answer', 'intent_summary': '測試', 'state': state or {}, 'rewritten_queries': [],
                       'selected': [{'doc_code': chunk.doc_code, 'sections': [chunk.section_path]}]}, ensure_ascii=False)


def route_clarify(state=None, safety_note=None):
    return json.dumps({'action': 'clarify', 'intent_summary': '測試', 'state': state or {'topic': '清潔'},
                       'clarify': {'dimension': 'line', 'question': '哪一條產線？', 'options': ['CL-01', 'CL-02'],
                                   'safety_note': safety_note}}, ensure_ascii=False)


ROUTE_OUT_OF_SCOPE = json.dumps({'action': 'out_of_scope', 'intent_summary': '閒聊'})


def answer_reply(chunk_ids, text='燈管使用 1,200 小時更換', condition=None, safety=None):
    return json.dumps({'status': 'answered', 'missing': None,
                       'safety_note': {'text': '先停機上鎖', 'citations': list(safety)} if safety else None,
                       'points': [{'text': text, 'citations': list(chunk_ids), 'condition': condition}]}, ensure_ascii=False)


def ask(client, headers, message, conversation_id=None, **extra):
    return client.post('/api/chat', headers=headers, json={'conversation_id': conversation_id, 'message': message, **extra})


def assert_error(response, status):
    assert response.status_code == status, response.text
    assert response.json()['error'] is True and response.json()['message']
    return response.json()['message']


def prompt_text(call):
    return call['system'] + '\n'.join(m['content'] for m in call['messages'])


# ---- chat flow -----------------------------------------------------------------------------

def test_new_conversation_stores_both_messages_records_calls_and_links_citations(chat_app, fakes):
    client, db, _, _ = chat_app
    user_id, headers = new_user(chat_app)
    chunk = first_chunk(db, 'MFG-SOP-002')
    fakes([route_answer(chunk)], [answer_reply([chunk.chunk_id], safety=[chunk.chunk_id])])
    response = ask(client, headers, 'CL-01 塗布頭要怎麼清潔？')
    assert response.status_code == 200, response.text
    body = response.json()
    assert body['conversation']['title'] == 'CL-01 塗布頭要怎麼清潔？'
    message = body['message']
    assert message['kind'] == 'answer' and message['feedback'] is None
    citation = message['payload']['points'][0]['citations'][0]
    doc_id = db.query('SELECT id FROM documents WHERE document_code=%s', ('MFG-SOP-002',), one=True)['id']
    assert {k: citation[k] for k in ('chunk_id', 'doc_code', 'doc_title', 'section_path')} == {
        'chunk_id': chunk.chunk_id, 'doc_code': 'MFG-SOP-002', 'doc_title': chunk.doc_title, 'section_path': chunk.section_path}
    assert citation['url'].startswith(f'/documents/{doc_id}?section=')
    assert message['payload']['safety_note']['citations'][0]['url'] == citation['url']
    assert set(body['quota']) == {'unlimited', 'percent_used', 'remaining_questions', 'resets_at'}

    rows = db.query('SELECT * FROM messages WHERE conversation_id=%s ORDER BY id', (body['conversation']['id'],))
    assert [(r['role'], r['kind']) for r in rows] == [('user', None), ('assistant', 'answer')]
    assert '1,200 小時' in rows[1]['history_text'] and chunk.chunk_id not in rows[1]['history_text']
    assert json.loads(rows[1]['trace'])['chunk_ids'] and 'url' not in rows[1]['payload']  # links are made when read
    calls = db.query('SELECT * FROM llm_calls WHERE user_id=%s ORDER BY id', (user_id,))
    assert [(c['purpose'], c['message_id'], c['error']) for c in calls] == [('router', rows[0]['id'], None), ('answer', rows[0]['id'], None)]
    assert all(c['budget_cost_usd'] > 0 and c['cost_usd'] is None for c in calls)  # fake models are unpriced

    listed = client.get(f'/api/conversations/{body["conversation"]["id"]}/messages', headers=headers).json()['data']
    assert [m['role'] for m in listed['messages']] == ['user', 'assistant']
    assert all('trace' not in m and 'history_text' not in m for m in listed['messages'])
    assert listed['messages'][1]['payload']['points'][0]['citations'][0]['url'] == citation['url']
    assert client.get('/api/conversations', headers=headers).json()['data'][0]['id'] == body['conversation']['id']


def test_clarify_state_lives_in_the_database_and_history_continues_after_the_answer(chat_app, fakes):
    client, db, _, _ = chat_app
    _, headers = new_user(chat_app)
    chunk = first_chunk(db, 'MFG-SOP-002')
    fake = fakes([route_clarify(), route_answer(chunk, {'line': 'CL-01'}), ROUTE_OUT_OF_SCOPE],
                 [answer_reply([chunk.chunk_id], condition='CL-01')])
    first = ask(client, headers, '塗布頭怎麼清潔？').json()
    conversation_id = first['conversation']['id']
    assert first['message']['kind'] == 'clarify'
    assert first['message']['payload'] == {'question': '哪一條產線？', 'options': ['CL-01', 'CL-02'], 'safety_note': None}
    row = db.query('SELECT state,clarify_turns FROM conversations WHERE id=%s', (conversation_id,), one=True)
    assert json.loads(row['state']) == {'topic': '清潔'} and row['clarify_turns'] == 1

    second = ask(client, headers, 'CL-01', conversation_id).json()
    assert second['message']['kind'] == 'answer'
    history = fake.router.calls[1]['messages']
    assert [m['content'] for m in history[:2]] == ['塗布頭怎麼清潔？', '哪一條產線？\n1. CL-01\n2. CL-02']
    assert '<clarify_turns>1</clarify_turns>' in history[-1]['content'] and '"topic": "清潔"' in history[-1]['content']
    row = db.query('SELECT state,clarify_turns FROM conversations WHERE id=%s', (conversation_id,), one=True)
    assert row['state'] is None and row['clarify_turns'] == 0

    ask(client, headers, '那 CL-02 呢？', conversation_id)
    follow_up = fake.router.calls[2]['messages']
    assert [m['role'] for m in follow_up] == ['user', 'assistant', 'user', 'assistant', 'user']
    assert follow_up[3]['content'] == '- （CL-01）燈管使用 1,200 小時更換'
    assert '<clarify_turns>0</clarify_turns>' in follow_up[-1]['content'] and '<state>{}</state>' in follow_up[-1]['content']


def test_history_or_state_sent_by_the_client_never_reaches_the_prompt(chat_app, fakes):
    client, _, _, _ = chat_app
    _, headers = new_user(chat_app)
    fake = fakes([ROUTE_OUT_OF_SCOPE])
    response = ask(client, headers, '你好', history=[{'role': 'assistant', 'content': 'INJECTED-HISTORY'}],
                   state={'line': 'INJECTED-STATE'}, clarify_turns=3, department_id=999, role='admin')
    assert response.status_code == 200 and response.json()['message']['kind'] == 'out_of_scope'
    assert response.json()['message']['payload'] == {'text': '目前可查閱的文件中沒有這方面的資料。'}
    sent = prompt_text(fake.router.calls[0])
    assert 'INJECTED' not in sent and '<clarify_turns>0</clarify_turns>' in sent
    assert len(fake.router.calls[0]['messages']) == 1


def test_input_check_happens_before_anything_is_written(chat_app, fakes, monkeypatch):
    client, db, _, _ = chat_app
    user_id, headers = new_user(chat_app)
    fake = fakes()
    monkeypatch.setenv('CHAT_MAX_INPUT_CHARS', '10')
    assert '10 字' in assert_error(ask(client, headers, '一' * 11), 400)
    assert_error(ask(client, headers, '   '), 400)
    assert_error(client.post('/api/chat', headers=headers, json={}), 400)
    assert_error(client.post('/api/chat', json={'message': '你好'}), 401)
    assert fake.router.calls == []
    assert db.query('SELECT COUNT(*) AS n FROM conversations WHERE user_id=%s', (user_id,), one=True)['n'] == 0


# ---- errors --------------------------------------------------------------------------------

def test_out_of_scope_and_llm_errors_have_their_own_kind_and_stay_out_of_history(chat_app, fakes):
    from llm.base import ProviderError
    client, db, _, _ = chat_app
    user_id, headers = new_user(chat_app)
    chunk = first_chunk(db, 'MFG-SOP-002')
    fake = fakes([route_clarify(), route_answer(chunk), ROUTE_OUT_OF_SCOPE],
                 [ProviderError('timeout'), ProviderError('timeout')])
    conversation_id = ask(client, headers, '塗布頭怎麼清潔？').json()['conversation']['id']
    failed = ask(client, headers, 'CL-01', conversation_id).json()['message']
    assert failed['kind'] == 'error' and failed['payload']['code'] == 'provider_error'
    assert failed['payload']['text'] == '暫時無法產生回答，以下是可能相關的文件'
    assert failed['payload']['related_docs'][0]['doc_code'] == 'MFG-SOP-002' and failed['payload']['related_docs'][0]['url']
    # The error left the clarify state alone, and the failed exchange is not in the next prompt.
    row = db.query('SELECT state,clarify_turns FROM conversations WHERE id=%s', (conversation_id,), one=True)
    assert json.loads(row['state']) == {'topic': '清潔'} and row['clarify_turns'] == 1
    ask(client, headers, '還是不知道', conversation_id)
    assert [m['content'] for m in fake.router.calls[2]['messages'][:-1]] == ['塗布頭怎麼清潔？', '哪一條產線？\n1. CL-01\n2. CL-02']
    calls = db.query('SELECT purpose,error,attempts FROM llm_calls WHERE user_id=%s ORDER BY id', (user_id,))
    assert [(c['purpose'], c['error']) for c in calls] == [('router', None), ('router', None), ('answer', 'provider_error'),
                                                           ('router', None)]
    assert calls[2]['attempts'] == 2


def test_busy_and_budget_exceeded_still_list_related_documents(chat_app, fakes, monkeypatch):
    from llm.guard import ConcurrencyGate
    client, db, _, _ = chat_app
    user_id, headers = new_user(chat_app)
    full = ConcurrencyGate(limit=1)
    assert full.try_enter()
    fakes(gate=full)
    busy = ask(client, headers, '塗布頭清潔步驟').json()['message']
    assert busy['kind'] == 'error' and busy['payload']['code'] == 'busy'
    assert busy['payload']['text'] == '目前使用人數較多，請稍後再試' and busy['payload']['related_docs']

    fake = fakes()
    monkeypatch.setenv('LLM_DAILY_BUDGET_USD', '0.000001')  # today's recorded calls already exceed it
    over = ask(client, headers, '塗布頭清潔步驟').json()['message']
    assert over['kind'] == 'error' and over['payload']['code'] == 'budget_exceeded'
    assert over['payload']['text'] == '今日體驗額度已用完，以下是可能相關的文件'
    assert 0 < len(over['payload']['related_docs']) <= 5 and fake.router.calls == []
    errors = [r['error'] for r in db.query('SELECT error FROM llm_calls WHERE user_id=%s ORDER BY id', (user_id,))]
    assert errors == ['busy', 'budget_exceeded']


def test_a_failing_call_recorder_is_logged_and_never_fails_the_request(chat_app, caplog):
    client, _, _, _ = chat_app
    _, headers = new_user(chat_app)
    fake = Fakes([ROUTE_OUT_OF_SCOPE])

    def broken_recorder(result, purpose):
        raise sqlite3.OperationalError('disk full')
    client.app.state.chat_clients = lambda budget, recorder: fake(budget, broken_recorder)
    assert ask(client, headers, '你好').json()['message']['kind'] == 'out_of_scope'
    assert 'llm call recorder failed' in caplog.text


def test_default_clients_share_one_provider_per_name_and_need_both_models(monkeypatch):
    from routers import chat as chat_api
    monkeypatch.setattr(chat_api, '_providers', {})
    monkeypatch.setenv('LLM_ROUTER_MODEL', 'fake:router')
    monkeypatch.setenv('LLM_ANSWER_MODEL', '')
    with pytest.raises(RuntimeError, match='LLM_ANSWER_MODEL'):
        chat_api.default_clients(None, None)
    monkeypatch.setenv('LLM_ANSWER_MODEL', 'fake:answer')
    first, second = chat_api.default_clients(None, None), chat_api.default_clients(None, None)
    assert first[0].provider is first[1].provider is second[0].provider
    assert (first[0].purpose, first[1].model) == ('router', 'answer') and first[0] is not second[0]


def test_unexpected_failure_stores_an_error_message_and_returns_500(chat_app):
    client, db, _, _ = chat_app
    user_id, headers = new_user(chat_app)

    def broken(budget, recorder):
        raise RuntimeError('no model configured')
    client.app.state.chat_clients = broken
    assert assert_error(ask(client, headers, '燈管多久換？'), 500) == '系統暫時無法回答，請稍後再試'
    rows = db.query('SELECT m.role,m.kind,m.payload FROM messages m JOIN conversations c ON c.id=m.conversation_id '
                    'WHERE c.user_id=%s ORDER BY m.id', (user_id,))
    assert [(r['role'], r['kind']) for r in rows] == [('user', None), ('assistant', 'error')]
    assert json.loads(rows[1]['payload'])['code'] == 'internal'


# ---- access --------------------------------------------------------------------------------

def test_other_users_hidden_and_other_department_conversations_are_404(chat_app, fakes):
    client, db, _, departments = chat_app
    owner_id, owner = new_user(chat_app)
    _, stranger = new_user(chat_app)
    fakes([ROUTE_OUT_OF_SCOPE] * 3)
    mine = ask(client, owner, '你好').json()['conversation']['id']
    answer_id = db.query("SELECT id FROM messages WHERE conversation_id=%s AND role='assistant'", (mine,), one=True)['id']
    for response in [client.get(f'/api/conversations/{mine}/messages', headers=stranger), ask(client, stranger, '續問', mine),
                     client.post(f'/api/conversations/{mine}/hide', headers=stranger),
                     client.put(f'/api/messages/{answer_id}/feedback', headers=stranger, json={'rating': 'up'})]:
        assert_error(response, 404)

    hidden = ask(client, owner, '另一段').json()['conversation']['id']
    assert client.post(f'/api/conversations/{hidden}/hide', headers=owner).json() == {'ok': True}
    assert_error(client.get(f'/api/conversations/{hidden}/messages', headers=owner), 404)
    assert_error(ask(client, owner, '續問', hidden), 404)
    assert_error(client.post(f'/api/conversations/{hidden}/hide', headers=owner), 404)
    assert [c['id'] for c in client.get('/api/conversations', headers=owner).json()['data']] == [mine]

    # Moved without going through the API (so nothing was hidden): the old department's conversation is still out of reach.
    db.execute('UPDATE users SET department_id=%s WHERE id=%s', (departments['QA'], owner_id))
    assert_error(client.get(f'/api/conversations/{mine}/messages', headers=owner), 404)
    assert client.get('/api/conversations', headers=owner).json()['data'] == []


def test_department_switch_hides_every_conversation_but_keeps_the_data(chat_app, fakes):
    """auth-reference 10: hidden from list, detail and follow-up; still in the database; other users untouched."""
    client, db, _, departments = chat_app
    user_id, headers = new_user(chat_app)
    _, other = new_user(chat_app)
    fakes([ROUTE_OUT_OF_SCOPE] * 3)
    ids = [ask(client, headers, q).json()['conversation']['id'] for q in ('一', '二')]
    others = ask(client, other, '三').json()['conversation']['id']
    response = client.patch('/api/auth/me/department', headers=headers,
                            json={'department_id': departments['QA'], 'confirm_clear_history': True})
    assert response.status_code == 200 and response.json()['hidden_conversations'] == 2
    assert client.get('/api/conversations', headers=headers).json()['data'] == []
    for conversation_id in ids:
        assert_error(client.get(f'/api/conversations/{conversation_id}/messages', headers=headers), 404)
        assert_error(ask(client, headers, '續問', conversation_id), 404)
    rows = db.query('SELECT hidden_reason FROM conversations WHERE user_id=%s', (user_id,))
    assert [r['hidden_reason'] for r in rows] == ['department_change'] * 2
    assert db.query('SELECT COUNT(*) AS n FROM messages WHERE conversation_id IN (%s,%s)', tuple(ids), one=True)['n'] == 4
    assert client.get(f'/api/conversations/{others}/messages', headers=other).status_code == 200


def test_department_stays_when_hiding_conversations_fails(chat_app, fakes, monkeypatch):
    """auth-reference 11: one transaction, so a failed hide rolls back the department change."""
    client, db, _, departments = chat_app
    user_id, headers = new_user(chat_app)
    fakes([ROUTE_OUT_OF_SCOPE])
    conversation_id = ask(client, headers, '你好').json()['conversation']['id']
    real_factory = db.connection_factory

    def failing_factory():
        connection = real_factory()
        real_cursor = connection.cursor

        def cursor(**kwargs):
            cur = real_cursor(**kwargs)
            real_execute = cur.execute

            def execute(sql, params=()):
                if sql.startswith('UPDATE conversations'):
                    raise sqlite3.OperationalError('simulated failure')
                return real_execute(sql, params)
            cur.execute = execute
            return cur
        connection.cursor = cursor
        return connection
    monkeypatch.setattr(db, 'connection_factory', failing_factory)
    response = client.patch('/api/auth/me/department', headers=headers,
                            json={'department_id': departments['QA'], 'confirm_clear_history': True})
    monkeypatch.setattr(db, 'connection_factory', real_factory)
    assert response.status_code == 503
    assert db.query('SELECT department_id FROM users WHERE id=%s', (user_id,), one=True)['department_id'] == departments['MFG']
    assert db.query('SELECT hidden_at FROM conversations WHERE id=%s', (conversation_id,), one=True)['hidden_at'] is None


# ---- cost guards ---------------------------------------------------------------------------

def insert_call(db, user_id, cost, created_at, message_id=None):
    db.execute("INSERT INTO llm_calls(user_id,message_id,purpose,provider,model,budget_cost_usd,created_at) "
               "VALUES(%s,%s,'router','fake','router',%s,%s)", (user_id, message_id, cost, created_at))


def test_db_budget_survives_a_restart_and_follows_the_taipei_day(chat_app):
    from llm.guard import DbBudget, taipei_day_bounds
    from repositories import chat
    from datetime import timezone
    _, db, _, _ = chat_app
    user_id, _ = new_user(chat_app)
    assert taipei_day_bounds(datetime(2030, 1, 1, 16, 30, tzinfo=timezone.utc)) == (datetime(2030, 1, 1, 16), datetime(2030, 1, 2, 16))
    insert_call(db, user_id, 0.5, datetime(2030, 1, 1, 15, 59, 59))  # Taipei 2030-01-01 23:59:59
    insert_call(db, user_id, 0.25, datetime(2030, 1, 1, 16, 0, 0))  # Taipei 2030-01-02 00:00:00
    late = lambda: datetime(2030, 1, 1, 23, 0, tzinfo=timezone.utc)  # noqa: E731  Taipei 2030-01-02 07:00
    early = lambda: datetime(2030, 1, 1, 15, 0, tzinfo=timezone.utc)  # noqa: E731  Taipei 2030-01-01 23:00
    assert DbBudget(db, limit_usd=1, now=late).spent == 0.25 and DbBudget(db, limit_usd=1, now=early).spent == 0.5
    assert chat.user_spent_today(db, user_id, late()) == 0.25

    budget = DbBudget(db, limit_usd=0.3, now=late)
    budget.charge(0.04)
    assert budget.allows_call() and DbBudget(db, limit_usd=0.3, now=late).spent == 0.25  # pending is per request
    budget.charge(0.01)
    assert not budget.allows_call()
    restarted = DbBudget(db, limit_usd=0.25, now=late)
    assert restarted.spent == 0.25 and not restarted.allows_call()


def test_user_quota_refuses_without_calling_the_model_and_admins_are_exempt(chat_app, fakes):
    from repositories import chat
    client, db, _, _ = chat_app
    user_id, headers = new_user(chat_app)
    admin_id, admin = new_user(chat_app, 'ADM', role='admin')
    fake = fakes([ROUTE_OUT_OF_SCOPE])
    for uid in (user_id, admin_id):
        insert_call(db, uid, 0.06, chat.utc_now())
    assert assert_error(ask(client, headers, '燈管多久換？'), 429) == '今日額度已用完，明天再來問吧'
    assert fake.router.calls == []
    assert db.query('SELECT COUNT(*) AS n FROM conversations WHERE user_id=%s', (user_id,), one=True)['n'] == 0
    quota = client.get('/api/chat/quota', headers=headers).json()['data']
    assert quota['percent_used'] == 100 and quota['remaining_questions'] == 0 and quota['unlimited'] is False
    assert ask(client, admin, '你好').status_code == 200
    assert client.get('/api/chat/quota', headers=admin).json()['data']['unlimited'] is True


def test_remaining_questions_use_the_weekly_average_or_the_default(chat_app, monkeypatch):
    from datetime import timezone
    from repositories import chat
    from routers import chat as chat_api
    _, db, _, _ = chat_app
    user_id, _ = new_user(chat_app)
    future, day = datetime(2031, 6, 1, 4, 0, tzinfo=timezone.utc), datetime(2031, 5, 30)
    assert chat.average_question_cost(db, now=future) is None
    conversation = chat.create_conversation(db, {'id': user_id, 'department_id': 1}, '平均')
    # clarify → answer is one question at the cost of both turns; the error and an older answer do not count.
    for role, kind, created_at in [('user', None, day), ('assistant', 'clarify', day), ('user', None, day),
                                   ('assistant', 'answer', day), ('user', None, day), ('assistant', 'out_of_scope', day),
                                   ('user', None, day), ('assistant', 'error', day),
                                   ('user', None, datetime(2031, 5, 1)), ('assistant', 'answer', datetime(2031, 5, 1))]:
        db.execute("INSERT INTO messages(conversation_id,role,content,kind,created_at) VALUES(%s,%s,'x',%s,%s)",
                   (conversation['id'], role, kind, created_at))
    for cost in (0.002, 0.003, 0.001, 0.0):  # clarify, answer, out_of_scope, error (refused, nothing charged)
        insert_call(db, user_id, cost, day)
    insert_call(db, user_id, 0.5, datetime(2031, 5, 1))  # older than 7 days
    assert chat.average_question_cost(db, now=future) == pytest.approx(0.003)  # 0.006 / 2 finished questions

    user = {'id': user_id, 'role': 'user'}
    monkeypatch.setattr(chat, 'user_spent_today', lambda db, uid, now=None: 0.011)
    monkeypatch.setattr(chat, 'average_question_cost', lambda db, now=None: 0.003)
    assert chat_api.quota(db, user)['remaining_questions'] == 13 and chat_api.quota(db, user)['percent_used'] == 22
    monkeypatch.setattr(chat, 'average_question_cost', lambda db, now=None: None)
    assert chat_api.quota(db, user)['remaining_questions'] == 15  # 0.039 / 0.0025


def test_a_second_request_from_the_same_user_is_refused_while_one_runs(chat_app):
    from llm.base import ProviderReply
    from llm.client import LLM
    from llm.guard import ConcurrencyGate
    from llm.pricing import Pricing
    client, _, _, _ = chat_app
    _, headers = new_user(chat_app)
    started, release = threading.Event(), threading.Event()

    class Slow:
        name = 'fake'

        def complete(self, **kwargs):
            started.set()
            release.wait(10)
            return ProviderReply(ROUTE_OUT_OF_SCOPE, 100, 50)
    client.app.state.chat_clients = lambda budget, recorder: tuple(
        LLM(Slow(), 'fake:x', purpose=p, pricing=Pricing({}), budget=budget, gate=ConcurrencyGate(5), recorder=recorder)
        for p in ('router', 'answer'))
    results = []
    worker = threading.Thread(target=lambda: results.append(ask(client, headers, '第一題')))
    worker.start()
    try:
        assert started.wait(10)
        assert assert_error(ask(client, headers, '第二題'), 429) == '上一個問題還在處理中'
    finally:
        release.set()
        worker.join(10)
    assert results[0].status_code == 200


# ---- feedback and admin --------------------------------------------------------------------

def test_feedback_only_on_own_answers_and_resending_updates_it(chat_app, fakes):
    client, db, _, _ = chat_app
    user_id, headers = new_user(chat_app)
    chunk = first_chunk(db, 'MFG-SOP-002')
    fakes([route_clarify(), route_answer(chunk)], [answer_reply([chunk.chunk_id])])
    clarify = ask(client, headers, '塗布頭怎麼清潔？').json()
    answer = ask(client, headers, 'CL-01', clarify['conversation']['id']).json()
    assert_error(client.put(f'/api/messages/{clarify["message"]["id"]}/feedback', headers=headers, json={'rating': 'up'}), 404)
    assert_error(client.put(f'/api/messages/{answer["message"]["id"]}/feedback', headers=headers, json={'rating': 'meh'}), 400)
    url = f'/api/messages/{answer["message"]["id"]}/feedback'
    assert client.put(url, headers=headers, json={'rating': 'up'}).status_code == 200
    assert client.put(url, headers=headers, json={'rating': 'down', 'comment': '  沒寫到 CL-01  '}).json()['data'] == {
        'rating': 'down', 'comment': '沒寫到 CL-01'}
    rows = db.query('SELECT rating,comment FROM feedback WHERE user_id=%s', (user_id,))
    assert rows == [{'rating': 'down', 'comment': '沒寫到 CL-01'}]
    messages = client.get(f'/api/conversations/{clarify["conversation"]["id"]}/messages', headers=headers).json()['data']['messages']
    assert messages[-1]['feedback'] == {'rating': 'down', 'comment': '沒寫到 CL-01'} and 'feedback' not in messages[1]


def test_a_double_click_racing_on_the_unique_key_updates_instead_of_failing(chat_app, fakes, monkeypatch):
    from repositories import chat
    client, db, _, _ = chat_app
    user_id, headers = new_user(chat_app)
    chunk = first_chunk(db, 'MFG-SOP-002')
    fakes([route_answer(chunk)], [answer_reply([chunk.chunk_id])])
    message_id = ask(client, headers, '燈管多久換？').json()['message']['id']
    url = f'/api/messages/{message_id}/feedback'
    assert client.put(url, headers=headers, json={'rating': 'up'}).status_code == 200
    real_update = chat._update_feedback
    calls = []

    def first_update_misses(*args):
        # The other click's INSERT lands between this request's UPDATE and INSERT.
        calls.append(args)
        return False if len(calls) == 1 else real_update(*args)
    monkeypatch.setattr(chat, '_update_feedback', first_update_misses)
    assert client.put(url, headers=headers, json={'rating': 'down'}).status_code == 200
    assert len(calls) == 2
    assert db.query('SELECT rating FROM feedback WHERE user_id=%s', (user_id,)) == [{'rating': 'down'}]


def test_admin_sees_hidden_conversations_traces_and_feedback(chat_app, fakes):
    client, db, _, departments = chat_app
    user_id, headers = new_user(chat_app)
    _, admin = new_user(chat_app, 'ADM', role='admin')
    chunk = first_chunk(db, 'MFG-SOP-002')
    fakes([route_answer(chunk)], [answer_reply([chunk.chunk_id])])
    sent = ask(client, headers, '燈管多久換？').json()
    client.put(f'/api/messages/{sent["message"]["id"]}/feedback', headers=headers, json={'rating': 'down', 'comment': '太短'})
    client.patch('/api/auth/me/department', headers=headers, json={'department_id': departments['QA'], 'confirm_clear_history': True})

    for path in ('/api/admin/users', f'/api/admin/users/{user_id}/conversations',
                 f'/api/admin/conversations/{sent["conversation"]["id"]}/messages', '/api/admin/stats'):
        assert_error(client.get(path, headers=headers), 403)
    listed = client.get(f'/api/admin/users/{user_id}/conversations', headers=admin).json()
    assert listed['data'][0]['hidden_reason'] == 'department_change' and listed['data'][0]['department']['name'] == '製造部'
    detail = client.get(f'/api/admin/conversations/{sent["conversation"]["id"]}/messages', headers=admin).json()
    reply = detail['messages'][1]
    assert reply['feedback'] == {'rating': 'down', 'comment': '太短'}
    assert reply['trace']['route_action'] == 'answer' and reply['trace']['selected_docs'] == ['MFG-SOP-002']
    assert reply['trace']['router']['model'] == 'fake:router' and reply['trace']['answer']['input_tokens'] == 100
    # Admin pages link every cited document to the admin document page, even outside the admin's department (ADM).
    doc_id = db.query('SELECT id FROM documents WHERE document_code=%s', ('MFG-SOP-002',), one=True)['id']
    assert reply['payload']['points'][0]['citations'][0]['url'].startswith(f'/admin/documents/{doc_id}?section=')
    users = {u['id']: u for u in client.get('/api/admin/users', headers=admin).json()['data']}
    assert users[user_id]['conversations'] == 1 and users[user_id]['last_active_at'] and users[user_id]['today_percent'] >= 0
    assert_error(client.get('/api/admin/stats?days=91', headers=admin), 400)
    assert client.get('/api/admin/stats?days=7', headers=admin).json()['data']['days'] == 7


def test_admins_read_any_published_document_only_through_the_admin_api(chat_app):
    """document_scope() never widens for admins: the department-wide APIs and the RAG scope stay as they were."""
    from rag import index
    client, db, _, departments = chat_app
    admin_id, admin = new_user(chat_app, 'ADM', role='admin')
    _, employee = new_user(chat_app)
    rd = db.query('SELECT id FROM documents WHERE document_code=%s', ('RD-RULE-001',), one=True)['id']
    db.execute("INSERT INTO documents(document_code,title,summary,content,category,department_id,visibility,status) "
               "VALUES('DRAFT-CHAT-1','草稿','摘要','內容','SOP',%s,'department','draft')", (departments['RD'],))
    draft = db.query('SELECT id FROM documents WHERE document_code=%s', ('DRAFT-CHAT-1',), one=True)['id']
    response = client.get(f'/api/admin/documents/{rd}', headers=admin)
    assert response.status_code == 200 and response.json()['data']['document_code'] == 'RD-RULE-001'
    assert 'content' in response.json()['data']
    assert_error(client.get(f'/api/admin/documents/{rd}', headers=employee), 403)
    assert_error(client.get(f'/api/admin/documents/{rd}'), 401)
    assert_error(client.get(f'/api/admin/documents/{draft}', headers=admin), 404)
    assert_error(client.get('/api/admin/documents/999999', headers=admin), 404)
    # The admin's own document APIs and RAG scope still follow the admin's department.
    assert_error(client.get(f'/api/documents/{rd}', headers=admin), 404)
    assert 'RD-RULE-001' not in index.visible_codes(db, {'id': admin_id, 'department_id': departments['ADM']})


def test_stats_match_the_inserted_records(tmp_path):
    from datetime import timezone
    from core.database import Database
    from repositories import stats
    from scripts.migrate import migrate
    db = Database.sqlite(tmp_path / 'stats.db')
    migrate(db, 'sqlite')
    day1, day2 = datetime(2030, 3, 1, 2), datetime(2030, 3, 2, 2)  # 10:00 Taipei on 3/1 and 3/2
    with db.transaction() as cursor:
        for row in [(1, 'MFG', '製造部'), (2, 'QA', '品保部')]:
            cursor.execute('INSERT INTO departments(id,code,name) VALUES(%s,%s,%s)', row)
        for row in [(1, 'a@x', '甲', 1, 'user', day1), (2, 'b@x', '乙', 2, 'user', day2),
                    (3, 'c@x', '丙', 1, 'admin', datetime(2029, 1, 1))]:
            cursor.execute("INSERT INTO users(id,email,name,password_hash,department_id,role,created_at) "
                           "VALUES(%s,%s,%s,'x',%s,%s,%s)", row)
        for row in [(1, 1, 1, day1), (2, 2, 2, day2), (3, 3, 1, day2)]:
            cursor.execute("INSERT INTO conversations(id,user_id,department_id,title,created_at,updated_at) "
                           "VALUES(%s,%s,%s,'t',%s,%s)", row + (row[3],))
        messages = [(1, 1, 'user', None, None, day1), (2, 1, 'assistant', 'answer', '{"status": "answered"}', day1),
                    (3, 2, 'user', None, None, day2), (4, 2, 'assistant', 'answer', '{"status": "partial"}', day2),
                    (5, 3, 'user', None, None, day2), (6, 3, 'assistant', 'out_of_scope', '{}', day2)]
        for row in messages:
            cursor.execute("INSERT INTO messages(id,conversation_id,role,content,kind,payload,created_at) "
                           "VALUES(%s,%s,%s,'問題',%s,%s,%s)", row)
        for row in [(1, 1, 'router', 0.01, day1), (1, 1, 'answer', 0.05, day1), (2, 3, 'router', 0.002, day2),
                    (2, 3, 'answer', 0.004, day2), (3, 5, 'router', 0.2, day2)]:
            cursor.execute("INSERT INTO llm_calls(user_id,message_id,purpose,provider,model,budget_cost_usd,created_at) "
                           "VALUES(%s,%s,%s,'fake','m',%s,%s)", row)
        for row in [(2, 1, 'up', None, day1), (4, 2, 'down', '漏了數值', day2)]:
            cursor.execute('INSERT INTO feedback(message_id,user_id,rating,comment,created_at,updated_at) '
                           'VALUES(%s,%s,%s,%s,%s,%s)', row + (row[4],))
    data = stats.dashboard(db, 2, user_limit=0.05, global_limit=2, now=datetime(2030, 3, 2, 5, tzinfo=timezone.utc))
    assert [d['date'] for d in data['daily']] == ['2030-03-01', '2030-03-02']
    assert [(d['questions'], d['new_users'], d['router_cost'], d['answer_cost'], d['capped_users'])
            for d in data['daily']] == [(1, 1, 0.01, 0.05, 1), (2, 1, 0.202, 0.004, 0)]
    assert data['feedback']['up'] == 1 and data['feedback']['down'] == 1 and data['feedback']['up_ratio'] == 0.5
    assert data['feedback']['by_department'] == [{'department': '品保部', 'up': 0, 'down': 1}, {'department': '製造部', 'up': 1, 'down': 0}]
    assert data['feedback']['by_status'] == [{'status': 'answered', 'up': 1, 'down': 0}, {'status': 'partial', 'up': 0, 'down': 1}]
    down = data['feedback']['down_list']
    assert len(down) == 1 and down[0]['question'] == '問題' and down[0]['comment'] == '漏了數值' and down[0]['conversation_id'] == 2
    usage = data['usage']
    assert usage['cost'] == 0.266 and usage['questions'] == 3 and usage['capped_users'] == 1  # the admin is not counted
    assert usage['avg_cost_per_question'] == pytest.approx(round(0.266 / 3, 4))
    assert usage['today_spent'] == 0.206 and usage['today_percent'] == 10.3
    assert [(u['user_id'], u['cost'], u['questions']) for u in data['top_users']] == [(3, 0.2, 1), (1, 0.06, 1), (2, 0.006, 1)]
