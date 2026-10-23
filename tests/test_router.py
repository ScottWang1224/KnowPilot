"""Router, glossary and clarify-dimension tests. Only the fake provider is used: no network, no API key."""
import hashlib
import json

import pytest


def make_client(script, *, budget_usd=100):
    from llm.client import LLM
    from llm.guard import ConcurrencyGate, DailyBudget
    from llm.pricing import Pricing
    from llm.providers.fake import FakeProvider
    provider = FakeProvider(script)
    client = LLM(provider, 'fake:router', purpose='router', pricing=Pricing({}),
                 budget=DailyBudget(limit_usd=budget_usd), gate=ConcurrencyGate(limit=5))
    return client, provider


def reply(action='answer', selected=(), **extra):
    return json.dumps({'action': action, 'intent_summary': '測試', 'state': {}, 'rewritten_queries': [],
                       'selected': [{'doc_code': code, 'sections': sections} for code, sections in selected],
                       **extra}, ensure_ascii=False)


CLARIFY = {'dimension': 'line', 'question': '哪一條產線？', 'options': ['CL-01', 'CL-02'], 'safety_note': None}


def user(departments, code):
    return {'id': 0, 'department_id': departments[code]}


def test_glossary_parses_terms_and_colloquial_phrases(seeded):
    from rag import glossary
    db, departments = seeded
    entries = {e['term']: e for e in glossary.load(db, user(departments, 'ADM'))}
    assert len(entries) >= 40 and 'SOP' not in entries  # the 文件與管理 table has no colloquial column
    assert entries['收卷不良']['colloquial'] == ['捲歪', '伸縮筒', '竹筍']
    assert entries['放卷／收卷']['colloquial'] == ['上料', '下料']
    assert '特採' not in entries['特採']['colloquial'] and entries['條紋']['meaning']
    text = glossary.format_for_prompt(entries.values())
    assert '收卷不良：捲歪、伸縮筒、竹筍' in text and '線狀痕跡' not in text


def test_glossary_is_empty_when_hidden_or_unparsable(seeded, monkeypatch, caplog):
    from rag import glossary
    db, departments = seeded
    monkeypatch.setattr(glossary, 'GLOSSARY_DOC_CODE', 'RD-RULE-001')  # stands in for a department-only glossary
    assert glossary.load(db, user(departments, 'MFG')) == []
    assert glossary.load(db, user(departments, 'RD')) == [] and 'no terms parsed' in caplog.text
    assert glossary.parse('| 名稱 | 說明 |\n| - | - |\n| a | b |') == []


def test_dimension_filter_needs_visible_matches_two_values_and_dependency():
    from rag.router import filter_dimensions
    dims = {
        'line': {'label': '產線', 'values': [{'value': 'CL-01', 'match': ['CL-01']}, {'value': 'CL-02', 'match': ['CL-02']},
                                             {'value': 'CL-09', 'match': ['CL-09']}]},
        'shift': {'label': '班別', 'values': [{'value': '早班', 'match': ['早班']}, {'value': '夜班', 'match': ['夜班']}]},
        'phenomenon': {'label': '現象', 'values': [{'value': 'D01', 'match': ['D01']}, {'value': 'D03', 'match': ['D03']}]},
        'pattern': {'label': '型態', 'depends_on': 'phenomenon',
                    'values': [{'value': '連續', 'match': ['連續']}, {'value': '間歇', 'match': ['間歇']}]},
    }
    kept = filter_dimensions(dims, 'ＣＬ-01 與 CL-02 早班 D01 D03 連續 間歇')  # full-width text is NFKC-normalized
    assert [v['value'] for v in kept['line']['values']] == ['CL-01', 'CL-02'] and 'shift' not in kept
    assert 'pattern' in kept
    assert set(filter_dimensions(dims, 'CL-01 CL-02 D01 連續 間歇')) == {'line'}


def test_real_dimensions_follow_department_visibility(seeded):
    from rag.router import visible_dimensions
    db, departments = seeded
    mfg = visible_dimensions(db, user(departments, 'MFG'))
    assert {'line', 'product', 'phenomenon', 'pattern', 'shift'} <= set(mfg)
    assert 'HC-400' not in json.dumps(mfg, ensure_ascii=False)


def test_prompt_never_contains_other_department_documents(seeded):
    from rag import router
    db, departments = seeded
    client, provider = make_client([reply('clarify', clarify=CLARIFY)])
    router.route(db, user(departments, 'MFG'), 'RF-25 配方 觸媒 白金 添加量', client=client)
    sent = provider.calls[0]['system'] + json.dumps(provider.calls[0]['messages'], ensure_ascii=False)
    hidden = db.query("SELECT d.document_code,d.title FROM documents d JOIN departments dep ON dep.id=d.department_id "
                      "WHERE dep.code='RD' AND d.visibility='department'")
    assert hidden and not [r for r in hidden if r['document_code'] in sent or r['title'] in sent]
    assert 'MFG-SOP-001' in sent and '收卷不良：' in sent


def test_invisible_selected_docs_are_dropped(seeded):
    from rag import router
    db, departments = seeded
    client, _ = make_client([reply(selected=[('RD-RULE-001', []), ('MFG-SOP-001', [])])])
    result = router.route(db, user(departments, 'MFG'), 'UV 燈管多久要換？', client=client)
    assert result.error is None and result.dropped_docs == ['RD-RULE-001']
    assert [s.doc_code for s in result.decision.selected] == ['MFG-SOP-001']
    assert result.chunks and not any(h.doc_code.startswith('RD-') for h in result.chunks)


def test_announcements_pass_visibility_check(seeded):
    from rag import router
    db, departments = seeded
    code = 'ANN:' + db.query("SELECT seed_key FROM announcements WHERE seed_key='typhoon-rule'", one=True)['seed_key']
    client, _ = make_client([reply(selected=[(code, [])])])
    result = router.route(db, user(departments, 'PE'), '颱風天要上班嗎？', client=client)
    assert result.dropped_docs == [] and result.chunks[0].doc_code == code


def test_clarify_after_three_turns_becomes_answer(seeded):
    from rag import router
    db, departments = seeded
    client, _ = make_client([reply('clarify', selected=[('MFG-SOP-009', [])], clarify=CLARIFY)] * 2)
    asked = router.route(db, user(departments, 'MFG'), '條紋又出現了', clarify_turns=2, client=client)
    assert asked.action == 'clarify' and asked.chunks == [] and asked.decision.clarify.options == ['CL-01', 'CL-02']
    forced = router.route(db, user(departments, 'MFG'), '不知道', clarify_turns=3, client=client)
    assert forced.action == 'answer' and forced.decision.multiple_possibilities and forced.decision.clarify is None
    assert {h.doc_code for h in forced.chunks} >= {'MFG-SOP-009'}


def test_invalid_output_twice_falls_back_to_bm25(seeded):
    from rag import index, router
    db, departments = seeded
    mfg = user(departments, 'MFG')
    one_option = reply('clarify', clarify={**CLARIFY, 'options': ['CL-01']})  # fails the clarify validator
    client, provider = make_client(['不是 JSON', one_option])
    result = router.route(db, mfg, 'UV 燈管多久要換？', client=client)
    assert len(provider.calls) == 2 and result.error == 'validation_failed'
    assert result.action == 'answer' and result.fallback == 'llm_error' and result.decision is None
    assert [h.chunk_id for h in result.chunks] == [h.chunk_id for h in index.search(db, mfg, 'UV 燈管多久要換？', k=5)]


def test_budget_used_up_still_returns_bm25_chunks(seeded):
    from rag import router
    db, departments = seeded
    client, provider = make_client([reply()], budget_usd=0)
    result = router.route(db, user(departments, 'MFG'), 'UV 燈管多久要換？', client=client)
    assert provider.calls == [] and result.error == 'budget_exceeded' and result.fallback == 'llm_error'
    assert len(result.chunks) == 5


def test_too_long_question_never_calls_the_model(seeded, monkeypatch):
    from rag import router
    db, departments = seeded
    monkeypatch.setenv('CHAT_MAX_INPUT_CHARS', '10')
    client, provider = make_client([reply()])
    result = router.route(db, user(departments, 'MFG'), '條' * 11, client=client)
    assert provider.calls == [] and result.error == 'invalid_input' and result.chunks == [] and '上限' in result.message


def test_sections_match_by_path_or_fall_back_to_best_bm25_chunks(seeded):
    from rag import index, router
    db, departments = seeded
    mfg = user(departments, 'MFG')
    paths = [c.section_path for c in index.get_index(db).chunks if c.doc_code == 'MFG-SOP-001']
    client, _ = make_client([reply(selected=[('MFG-SOP-001', [' ' + paths[1]])]),
                             reply(selected=[('MFG-SOP-001', ['99. 不存在的章節'])])])
    matched = router.route(db, mfg, 'UV 燈管多久要換？', client=client)
    assert [h.section_path for h in matched.chunks] == [paths[1]]
    missed = router.route(db, mfg, 'UV 燈管多久要換？', client=client)
    best = index.search(db, mfg, 'UV 燈管多久要換？', k=2, restrict_docs={'MFG-SOP-001'})
    assert [h.chunk_id for h in missed.chunks] == [h.chunk_id for h in best]


def test_prompt_hash_identifies_the_prompt_file_not_the_department(seeded):
    from rag import router
    db, departments = seeded
    expected = hashlib.sha256(router.PROMPT_FILE.read_text(encoding='utf-8').encode()).hexdigest()[:12]
    hashes = set()
    for code in ('MFG', 'RD'):
        client, provider = make_client([reply('out_of_scope')])
        result = router.route(db, user(departments, code), 'CL-03 開機程序', client=client)
        assert result.action == 'out_of_scope' and result.chunks == [] and result.llm.prompt_name == 'router.md'
        hashes.add(result.llm.prompt_hash)
    assert hashes == {expected}


def test_route_without_client_requires_a_router_model(seeded, monkeypatch):
    from rag import router
    db, departments = seeded
    monkeypatch.delenv('LLM_ROUTER_MODEL', raising=False)
    with pytest.raises(RuntimeError, match='LLM_ROUTER_MODEL'):
        router.route(db, user(departments, 'MFG'), '測試')
