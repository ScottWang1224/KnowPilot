"""Chat API: each question is one request, routed and answered by rag.pipeline and stored in the database.

History, routing state and clarify turns are always read from the database, never from the
request, so a client cannot put words in the assistant's mouth. Endpoints are plain `def`:
routing and answering block, so FastAPI runs them in its threadpool.
"""
import logging
import math
import threading
from contextlib import contextmanager
from typing import Annotated
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Path, Request

from core.dependencies import get_current_user
from core.views import utc_iso
from llm import guard, settings
from llm.client import LLM, provider_for
from rag import answer as answers
from rag.pipeline import Conversation, clarify_text, history_text, run_turn
from repositories import chat
from schemas.chat import ChatRequest, FeedbackRequest

log = logging.getLogger('knowpilot.chat')
router = APIRouter(prefix='/api')

ERROR_TEXTS = {'budget_exceeded': '今日體驗額度已用完，以下是可能相關的文件', 'busy': '目前使用人數較多，請稍後再試'}
OTHER_ERROR_TEXT = '暫時無法產生回答，以下是可能相關的文件'
INTERNAL_ERROR_TEXT = '系統暫時無法回答，請稍後再試'
ERROR_CODES = {'budget_exceeded', 'busy', 'provider_error', 'validation_failed'}
QUOTA_USED_UP = '今日額度已用完，明天再來問吧'
IN_PROGRESS = '上一個問題還在處理中'
NOT_FOUND = '找不到這段對話'
RELATED_DOCS = 5
SECTION_SEPARATOR = ' › '

_active, _active_lock = set(), threading.Lock()


@contextmanager
def one_request_per_user(user_id):
    """Held while a question runs. In process memory: assumes a single uvicorn worker."""
    with _active_lock:
        if user_id in _active:
            raise HTTPException(429, IN_PROGRESS)
        _active.add(user_id)
    try:
        yield
    finally:
        with _active_lock:
            _active.discard(user_id)


_providers, _providers_lock = {}, threading.Lock()


def shared_provider(spec):
    """One provider per name for the process, so the SDK's HTTP connections are reused between requests."""
    name = spec.split(':', 1)[0]
    with _providers_lock:
        if name not in _providers:
            _providers[name] = provider_for(name)
        return _providers[name]


def warm_up():
    """Import the configured providers' SDKs at startup (anthropic alone takes seconds), not on the first question."""
    for spec in (settings.router_model(), settings.answer_model()):
        if ':' in spec:
            shared_provider(spec)


def default_clients(budget, recorder):
    """(router client, answer client) for one request. Installed as app.state.chat_clients; tests swap in fakes."""
    specs = (settings.router_model(), settings.answer_model())
    if not all(':' in spec for spec in specs):
        raise RuntimeError('聊天 API 需要設定 LLM_ROUTER_MODEL 與 LLM_ANSWER_MODEL（格式：供應商:模型 ID）')
    return tuple(LLM(shared_provider(spec), spec, purpose=purpose, budget=budget, recorder=recorder)
                 for spec, purpose in zip(specs, ('router', 'answer')))


# ---- quota ---------------------------------------------------------------------------------

def quota(db, user):
    """Today's per-user allowance as a percentage and an estimated number of questions; never amounts."""
    resets_at = utc_iso(guard.taipei_day_bounds()[1])
    if user['role'] == 'admin':
        return {'unlimited': True, 'percent_used': None, 'remaining_questions': None, 'resets_at': resets_at}
    limit = settings.chat_user_daily_budget_usd()
    spent = chat.user_spent_today(db, user['id'])
    per_question = chat.average_question_cost(db) or settings.chat_default_cost_per_question_usd()
    remaining = max(0.0, limit - spent)
    # Floor (with a float-error margin) so 100% and "0 questions" only show once the allowance is truly used up.
    return {'unlimited': False,
            'percent_used': min(100, math.floor(spent / limit * 100 + 1e-9)) if limit > 0 else 100,
            'remaining_questions': math.floor(remaining / per_question + 1e-9),
            'resets_at': resets_at}


def quota_used_up(db, user):
    return user['role'] != 'admin' and chat.user_spent_today(db, user['id']) >= settings.chat_user_daily_budget_usd()


# ---- turning a pipeline result into a stored message ---------------------------------------

def related_docs(route):
    docs = {}
    for hit in route.chunks:
        docs.setdefault(hit.doc_code, {'doc_code': hit.doc_code, 'doc_title': hit.doc_title})
    return list(docs.values())[:RELATED_DOCS]


def reply_for(turn):
    """(kind, content, payload) of the assistant message."""
    route, result = turn.route, turn.answer
    if route.action == 'clarify':
        clarify = route.decision.clarify
        return 'clarify', clarify_text(clarify), {'question': clarify.question, 'options': clarify.options,
                                                  'safety_note': clarify.safety_note}
    if route.action == 'out_of_scope':
        return 'out_of_scope', answers.OUT_OF_SCOPE_TEXT, {'text': answers.OUT_OF_SCOPE_TEXT}
    if result is None or result.output is None:
        code = result.error if result and result.error in ERROR_CODES else 'internal'
        text = ERROR_TEXTS.get(code, OTHER_ERROR_TEXT)
        return 'error', text, {'code': code, 'text': text, 'related_docs': related_docs(route)}
    output, chunks = result.output, {hit.chunk_id: hit for hit in route.chunks}

    def cite(chunk_ids):
        # verify() already removed every citation outside route.chunks.
        return [{'chunk_id': c, 'doc_code': chunks[c].doc_code, 'doc_title': chunks[c].doc_title,
                 'section_path': chunks[c].section_path} for c in chunk_ids]

    safety = output.safety_note
    return 'answer', answers.format_answer(output), {
        'status': output.status, 'missing': output.missing,
        'text': answers.NOT_FOUND_TEXT if output.status == 'not_found' else None,
        'safety_note': {'text': safety.text, 'citations': cite(safety.citations)} if safety else None,
        'points': [{'text': p.text, 'condition': p.condition, 'citations': cite(p.citations)} for p in output.points]}


def llm_trace(result):
    if result is None:
        return None
    return {'model': f'{result.provider}:{result.model}', 'input_tokens': result.input_tokens,
            'output_tokens': result.output_tokens, 'latency_ms': result.latency_ms, 'attempts': result.attempts,
            'cost_usd': result.cost_usd, 'budget_cost_usd': round(result.budget_cost_usd, 6), 'error': result.error}


def trace_for(turn):
    """Routing and answering summary for the admin pages; never chunk text."""
    route, result = turn.route, turn.answer
    return {'route_action': route.action, 'route_error': route.error, 'fallback': route.fallback,
            'selected_docs': [s.doc_code for s in route.decision.selected] if route.decision else [],
            'chunk_ids': [hit.chunk_id for hit in route.chunks], 'dropped_docs': route.dropped_docs,
            'answer_status': result.status if result else None, 'answer_error': result.error if result else None,
            'removed_citations': result.removed_citations if result else [],
            'dropped_points': result.dropped_points if result else 0,
            'dropped_missing': result.dropped_missing if result else False,
            'router': llm_trace(route.llm), 'answer': llm_trace(result.llm if result else None)}


def with_links(db, reader, kind, payload, *, admin_view=False):
    """Add a url to each citation / related document the reader may open (their own document_scope;
    admin pages link every published document to the admin document page)."""
    if not payload:
        return payload
    if kind == 'answer':
        items = [c for p in payload['points'] for c in p['citations']]
        items += payload['safety_note']['citations'] if payload['safety_note'] else []
    elif kind == 'error':
        items = payload['related_docs']
    else:
        return payload
    links = chat.link_targets(db, reader, [item['doc_code'] for item in items], admin_view=admin_view)
    for item in items:
        url = links.get(item['doc_code'])
        if url and item.get('section_path') and not item['doc_code'].startswith('ANN:'):
            url += '?section=' + quote(item['section_path'].split(SECTION_SEPARATOR)[-1])
        item['url'] = url
    return payload


def message_data(db, reader, row, feedback=None, *, admin_view=False):
    data = {'id': row['id'], 'role': row['role'], 'content': row['content'], 'kind': row['kind'],
            'payload': with_links(db, reader, row['kind'], chat.loads(row['payload']), admin_view=admin_view),
            'created_at': utc_iso(row['created_at'])}
    if row['kind'] == 'answer':
        data['feedback'] = {'rating': feedback['rating'], 'comment': feedback['comment']} if feedback else None
    return data


def run_question(request, db, user, conversation, message_id, text):
    budget = guard.DbBudget(db)

    def recorder(result, purpose):
        chat.record_call(db, user_id=user['id'], message_id=message_id, purpose=purpose, result=result)
        budget.clear_pending()

    router_client, answer_client = request.app.state.chat_clients(budget, recorder)
    conv = Conversation(history=chat.history(db, conversation['id'], message_id),
                        state=chat.loads(conversation['state']), clarify_turns=conversation['clarify_turns'])
    turn = run_turn(db, user, text, conv, router_client=router_client, answer_client=answer_client,
                    keep_history_after_answer=True)
    kind, content, payload = reply_for(turn)
    failed = kind == 'error'
    # An error leaves the conversation's state and clarify turns as they were.
    reply_id = chat.add_assistant_message(db, conversation['id'], content=content,
                                          history_text=None if failed else history_text(turn), kind=kind,
                                          payload=payload, trace=trace_for(turn),
                                          state=None if failed else conv.state,
                                          clarify_turns=None if failed else conv.clarify_turns)
    return reply_id, kind, payload


# ---- endpoints -----------------------------------------------------------------------------

@router.post('/chat')
def send(request: Request, body: ChatRequest, user=Depends(get_current_user)):
    db = request.app.state.db
    problem = guard.check_question(body.message)
    if problem:
        raise HTTPException(400, problem)
    if quota_used_up(db, user):
        raise HTTPException(429, QUOTA_USED_UP)
    with one_request_per_user(user['id']):
        if body.conversation_id is None:
            conversation = chat.create_conversation(db, user, body.message)
        else:
            conversation = chat.visible_conversation(db, user, body.conversation_id)
            if conversation is None:
                raise HTTPException(404, NOT_FOUND)
        message_id = chat.add_user_message(db, conversation['id'], body.message)
        try:
            reply_id, kind, payload = run_question(request, db, user, conversation, message_id, body.message)
        except Exception:
            log.exception('chat turn failed')
            try:
                chat.add_assistant_message(db, conversation['id'], content=INTERNAL_ERROR_TEXT, history_text=None,
                                           kind='error', payload={'code': 'internal', 'text': INTERNAL_ERROR_TEXT,
                                                                  'related_docs': []}, trace=None)
            except Exception:
                log.exception('could not store the error message')
            raise HTTPException(500, INTERNAL_ERROR_TEXT)
    return {'conversation': {'id': conversation['id'], 'title': conversation['title']},
            'message': {'id': reply_id, 'kind': kind, 'payload': with_links(db, user, kind, payload),
                        **({'feedback': None} if kind == 'answer' else {})},
            'quota': quota(db, user)}


@router.get('/chat/quota')
def get_quota(request: Request, user=Depends(get_current_user)):
    return {'data': quota(request.app.state.db, user)}


@router.get('/conversations')
def conversations(request: Request, user=Depends(get_current_user)):
    return {'data': [{'id': c['id'], 'title': c['title'], 'created_at': utc_iso(c['created_at']),
                      'updated_at': utc_iso(c['updated_at'])} for c in chat.visible_conversations(request.app.state.db, user)]}


@router.get('/conversations/{conversation_id}/messages')
def conversation_messages(request: Request, conversation_id: Annotated[int, Path(ge=1)], user=Depends(get_current_user)):
    db = request.app.state.db
    conversation = chat.visible_conversation(db, user, conversation_id)
    if conversation is None:
        raise HTTPException(404, NOT_FOUND)
    rows = chat.messages(db, conversation_id)
    feedback = chat.feedback_for(db, [r['id'] for r in rows if r['kind'] == 'answer'], user_id=user['id'])
    return {'data': {'conversation': {'id': conversation['id'], 'title': conversation['title']},
                     'messages': [message_data(db, user, r, feedback.get(r['id'])) for r in rows]}}


@router.post('/conversations/{conversation_id}/hide')
def hide_conversation(request: Request, conversation_id: Annotated[int, Path(ge=1)], user=Depends(get_current_user)):
    if not chat.hide(request.app.state.db, user, conversation_id):
        raise HTTPException(404, NOT_FOUND)
    return {'ok': True}


@router.put('/messages/{message_id}/feedback')
def feedback(request: Request, message_id: Annotated[int, Path(ge=1)], body: FeedbackRequest,
             user=Depends(get_current_user)):
    db = request.app.state.db
    if chat.answer_message(db, user, message_id) is None:
        raise HTTPException(404, '找不到這則回答')
    chat.set_feedback(db, user, message_id, body.rating, body.comment)
    return {'data': {'rating': body.rating, 'comment': body.comment}}
