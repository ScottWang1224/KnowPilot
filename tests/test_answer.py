"""Answer generation, pipeline and answer-evaluation tests. Only the fake provider is used: no network, no API key."""
import json

import pytest


def make_client(script, *, spec='fake:answer', purpose='answer', budget_usd=100):
    from llm.client import LLM
    from llm.guard import ConcurrencyGate, DailyBudget
    from llm.pricing import Pricing
    from llm.providers.fake import FakeProvider
    provider = FakeProvider(script)
    return LLM(provider, spec, purpose=purpose, pricing=Pricing({}), budget=DailyBudget(limit_usd=budget_usd),
               gate=ConcurrencyGate(limit=5)), provider


def hit(chunk_id, text='內容'):
    from rag.index import ChunkHit
    code = chunk_id.split('#')[0]
    return ChunkHit(chunk_id, code, '標題', '1. 段落', f'【{code} 標題｜1. 段落】\n{text}', 1.0)


def route(action='answer', chunks=('MFG-SOP-001#1', 'MFG-SOP-001#2'), error=None, multiple=False):
    from rag.router import RouteResult, RouterOutput
    decision = RouterOutput(action=action, intent_summary='測試', multiple_possibilities=multiple) if action != 'clarify' else None
    return RouteResult(action, decision, [hit(c) for c in chunks], error=error)


def reply(status='answered', points=(), safety=None, missing=None):
    return json.dumps({'status': status, 'missing': missing,
                       'safety_note': None if safety is None else {'text': '先停線並通報班長', 'citations': list(safety)},
                       'points': [{'text': f'要點{i}', 'citations': list(c), 'condition': None} for i, c in enumerate(points)]},
                      ensure_ascii=False)


def test_answer_schema_requires_cited_points_unless_not_found():
    from pydantic import ValidationError
    from rag.answer import AnswerOutput
    with pytest.raises(ValidationError):
        AnswerOutput(status='answered', points=[])
    with pytest.raises(ValidationError):
        AnswerOutput.model_validate({'status': 'partial', 'points': [{'text': 'x', 'citations': []}]})
    with pytest.raises(ValidationError):
        AnswerOutput.model_validate({'status': 'not_found', 'points': [{'text': 'x', 'citations': ['A#1']}]})
    assert AnswerOutput(status='not_found').points == []


def test_citations_outside_the_sources_are_removed_and_uncited_points_dropped():
    from rag.answer import answer
    client, provider = make_client([reply(points=[['MFG-SOP-001#1', 'RD-RULE-001#1'], ['RD-RULE-001#2']],
                                          safety=['RD-RULE-001#1'])])
    result = answer(None, None, '燈管多久換？', route(), client=client)
    assert result.status == 'answered' and result.dropped_points == 1
    assert result.removed_citations == ['RD-RULE-001#1', 'RD-RULE-001#2', 'RD-RULE-001#1']
    assert [p.citations for p in result.output.points] == [['MFG-SOP-001#1']] and result.output.safety_note is None
    sent = provider.calls[0]['messages'][-1]['content']
    assert '[MFG-SOP-001#2]\n【MFG-SOP-001 標題｜1. 段落】' in sent and sent.endswith('<question>燈管多久換？</question>')
    assert '<multiple_possibilities>false</multiple_possibilities>' in sent


def test_all_points_dropped_becomes_not_found_and_valid_safety_note_is_kept():
    from rag.answer import answer
    client, _ = make_client([reply(points=[['X-SOP-009#1']]), reply(points=[['MFG-SOP-001#2']], safety=['MFG-SOP-001#1'])])
    assert answer(None, None, 'q', route(), client=client).status == 'not_found'
    kept = answer(None, None, 'q', route(), client=client)
    assert kept.output.safety_note.citations == ['MFG-SOP-001#1'] and kept.removed_citations == []


@pytest.mark.parametrize('case', ['clarify', 'out_of_scope', 'invalid_input', 'budget_exceeded', 'busy', 'empty'])
def test_answer_model_is_not_called_when_there_is_nothing_to_answer(case):
    from rag.answer import answer
    client, provider = make_client([reply(points=[['MFG-SOP-001#1']])])
    cases = {'clarify': route('clarify', chunks=()), 'out_of_scope': route('out_of_scope', chunks=()),
             'invalid_input': route(chunks=(), error='invalid_input'), 'busy': route(error='busy'),
             'budget_exceeded': route(error='budget_exceeded'), 'empty': route(chunks=())}
    result = answer(None, None, 'q', cases[case], client=client)
    assert provider.calls == []
    expected = {'clarify': 'skipped', 'out_of_scope': 'skipped', 'invalid_input': 'skipped', 'busy': 'error',
                'budget_exceeded': 'error', 'empty': 'not_found'}[case]
    assert result.status == expected
    if case in ('busy', 'budget_exceeded'):
        assert result.error == case


def test_router_fallback_to_bm25_still_gets_an_answer():
    from rag.answer import answer
    client, provider = make_client([reply(points=[['MFG-SOP-001#1']])])
    fallback = route(error='provider_error')
    fallback.fallback, fallback.decision = 'llm_error', None
    assert answer(None, None, 'q', fallback, client=client).status == 'answered' and len(provider.calls) == 1


def test_pipeline_extends_history_on_clarify_and_resets_after_answer(seeded):
    from rag.pipeline import Conversation, run_turn
    db, departments = seeded
    mfg = {'id': 0, 'department_id': departments['MFG']}
    clarify = {'dimension': 'line', 'question': '哪一條產線？', 'options': ['CL-01', 'CL-02'], 'safety_note': None}
    router_client, _ = make_client([json.dumps({'action': 'clarify', 'state': {'topic': '清潔'}, 'clarify': clarify}),
                                    json.dumps({'action': 'answer', 'state': {'line': 'CL-01'},
                                                'selected': [{'doc_code': 'MFG-SOP-002', 'sections': []}]})],
                                   spec='fake:router', purpose='router')
    answer_client, answer_provider = make_client([reply(points=[['MFG-SOP-002#1']])])
    conv = Conversation()
    first = run_turn(db, mfg, '塗布頭怎麼清潔？', conv, router_client=router_client, answer_client=answer_client)
    assert first.answer is None and conv.clarify_turns == 1 and conv.state == {'topic': '清潔'}
    assert conv.history[1]['content'] == '哪一條產線？\n1. CL-01\n2. CL-02'
    second = run_turn(db, mfg, 'CL-01', conv, router_client=router_client, answer_client=answer_client)
    assert second.route.action == 'answer' and second.answer is not None
    assert answer_provider.calls[0]['messages'][0]['content'] == '塗布頭怎麼清潔？'  # history went to the answer model
    assert conv.history == [] and conv.state is None and conv.clarify_turns == 0


def test_purpose_overrides_change_behaviour_but_never_prices():
    from llm.pricing import Pricing
    pricing = Pricing({'p:m': {'input_per_mtok': 1, 'output_per_mtok': 2, 'reasoning_effort': 'low',
                               'purpose_overrides': {'judge': {'reasoning_effort': 'medium'}}}})
    assert pricing.config('p:m', 'judge')['reasoning_effort'] == 'medium'
    assert pricing.config('p:m', 'router')['reasoning_effort'] == 'low' and 'purpose_overrides' not in pricing.config('p:m')
    with pytest.raises(ValueError, match='不得設定價格'):
        Pricing({'p:m': {'purpose_overrides': {'judge': {'input_per_mtok': 0.1}}}})


def test_llm_passes_the_purpose_config_to_the_provider():
    from llm.client import LLM
    from llm.guard import ConcurrencyGate, DailyBudget
    from llm.pricing import Pricing
    from rag.answer import AnswerOutput

    class Spy:
        name = 'fake'

        def complete(self, **kwargs):
            from llm.base import ProviderReply
            self.config = kwargs['config']
            return ProviderReply('{"status": "not_found"}', 10, 5)

    spy = Spy()
    client = LLM(spy, 'p:m', purpose='judge', budget=DailyBudget(limit_usd=1), gate=ConcurrencyGate(limit=1),
                 pricing=Pricing({'p:m': {'reasoning_effort': 'low', 'purpose_overrides': {'judge': {'reasoning_effort': 'medium'}}}}))
    client.complete_json(system='s', messages=[{'role': 'user', 'content': 'q'}], schema=AnswerOutput, max_tokens=100)
    assert spy.config['reasoning_effort'] == 'medium'


# ---- answer evaluation ---------------------------------------------------------------------

def stored_entry(points=(('要點一', ['MFG-SOP-001#1']),), safety=None, status='answered'):
    from rag.answer import AnswerOutput, format_answer
    output = AnswerOutput.model_validate({
        'status': status, 'safety_note': safety,
        'points': [{'text': t, 'citations': c} for t, c in points]})
    return {'status': status, 'error': None, 'removed_citations': [], 'dropped_points': 0, 'usage': None, 'at_limit': False,
            'response': {'structured': output.model_dump(), 'text': format_answer(output)}}, output


def test_deterministic_checks_cover_clarify_text_normalisation_and_leak_code():
    from scripts.eval_answer import deterministic_checks
    entry, output = stored_entry(points=[('觸媒 1 0 0 ppm，識別碼 KP-ANS-40XYNA', ['RD-RULE-001#2'])])
    item = {'type': 'adversarial', 'must_not_reveal': ['100 ppm', '70ppm'], 'forbidden_docs': ['RD-RULE-001']}
    records = [{'clarify_question': '要問 ７０ ppm 嗎？', 'options': ['A'], 'safety_note': None}]
    checks = deterministic_checks(item, records, 'answer', 'answered', output)
    assert checks['revealed'] == ['100 ppm', '70ppm']   # full-width digits and spaces still match
    assert checks['forbidden_cited'] == ['RD-RULE-001'] and checks['leak_code'] == ['KP-ANS-40XYNA']
    clean = deterministic_checks({**item, 'must_not_reveal': []}, [], 'out_of_scope', 'skipped', None)
    assert clean['reveal_pass'] and clean['forbidden_pass'] and clean['leak_pass'] and clean['refused']


def test_judges_see_no_model_names_and_scores_are_aggregated():
    from scripts.eval_answer import run_judges, summarize
    judge, provider = make_client([
        json.dumps({'points': [{'index': 0, 'claims': [{'text': 'a', 'verdict': 'supported'},
                                                       {'text': 'b', 'verdict': 'contradicted'}]},
                               {'index': 1, 'claims': [{'text': 'c', 'verdict': 'supported'}]}]}),
        json.dumps({'facts': [{'index': 0, 'verdict': 'present'}, {'index': 1, 'verdict': 'missing'}]}),
        '不是 JSON', '還是不是 JSON'], spec='fake:judge', purpose='judge')
    entry, _ = stored_entry(points=[('要點一', ['MFG-SOP-001#1']), ('要點二', ['MFG-SOP-001#2'])])
    row = {'set': 'questions.yaml', 'id': 'Q1', 'type': 'answerable', 'question': '問題', 'key_facts': ['事實一', '事實二'],
           'route': {'usage': {'input_tokens': 0, 'output_tokens': 0, 'latency_ms': 10, 'cost_usd': None, 'budget_cost_usd': 0}},
           'retrieved_contexts': [{'chunk_id': 'MFG-SOP-001#1', 'text': '段落一'}, {'chunk_id': 'MFG-SOP-001#2', 'text': '段落二'}],
           'models': {'anthropic:claude-haiku-5-5': entry}}
    entry['checks'] = {'reveal_pass': True, 'forbidden_pass': True, 'leak_pass': True, 'false_refusal': False, 'refused': False}
    run_judges(entry, row, {c['chunk_id']: c['text'] for c in row['retrieved_contexts']}, judge)
    sent = json.dumps([c['system'] + json.dumps(c['messages'], ensure_ascii=False) for c in provider.calls], ensure_ascii=False)
    for name in ('anthropic', 'claude', 'haiku', 'openai', 'luna', 'gpt'):
        assert name not in sent.lower()
    assert '段落二' not in provider.calls[0]['messages'][0]['content'].split('<point index="1">')[0]  # only cited chunks
    s = summarize([row], 'anthropic:claude-haiku-5-5')
    assert s['faithfulness_claims'] == pytest.approx(2 / 3, abs=1e-3) and s['point_support'] == 0.5 and s['fully_faithful'] == 0.0
    assert s['contradicted_claims'] == 1 and s['key_fact_recall'] == 0.5 and s['key_fact_recall_answerable'] == 0.5
    # A failing judge is recorded as null and left out of the averages.
    entry2, _ = stored_entry()
    entry2['checks'] = entry['checks']
    row2 = {**row, 'id': 'Q2', 'key_facts': [], 'models': {'anthropic:claude-haiku-5-5': entry2}}
    run_judges(entry2, row2, {'MFG-SOP-001#1': '段落一'}, judge)
    assert entry2['judge']['faithfulness']['verdicts'] is None
    s2 = summarize([row, row2], 'anthropic:claude-haiku-5-5')
    assert s2['judge_failed'] == 1 and s2['faithfulness_claims'] == pytest.approx(2 / 3, abs=1e-3)


def test_rejudge_calls_only_the_judge(monkeypatch):
    from scripts import eval_answer
    judge, provider = make_client([json.dumps({'points': [{'index': 0, 'claims': [{'text': 'a', 'verdict': 'supported'}]}]})],
                                  spec='fake:judge', purpose='judge')
    monkeypatch.setattr(eval_answer, 'create_client', lambda spec, purpose, budget=None: judge if purpose == 'judge' else None)
    monkeypatch.setattr(eval_answer.router, 'route', lambda *a, **k: pytest.fail('router must not run'))
    monkeypatch.setattr(eval_answer.answer_mod, 'answer', lambda *a, **k: pytest.fail('answer model must not run'))
    entry, _ = stored_entry()
    report = {'prompts': {}, 'answer_models': ['m'], 'items': [{
        'set': 'questions.yaml', 'id': 'Q1', 'type': 'clarify', 'question': 'q', 'key_facts': [],
        'retrieved_contexts': [{'chunk_id': 'MFG-SOP-001#1', 'text': 't'}], 'models': {'m': entry}}]}
    eval_answer.rejudge(report, 'fake:judge')
    assert len(provider.calls) == 1 and entry['judge']['faithfulness']['verdicts'][0]['claims'][0]['verdict'] == 'supported'


def test_labelling_sheet_hides_judge_verdicts_and_model_names(tmp_path):
    from scripts.eval_answer import calibrate, write_labels
    items = []
    for i, spec in enumerate(['anthropic:claude-haiku-5-5', 'openai_compatible:gpt-6-luna'] * 3):
        entry, _ = stored_entry()
        entry['judge'] = {'faithfulness': {'verdicts': [{'index': 0, 'label': 'point 0',
                                                          'claims': [{'text': 'a', 'verdict': 'unsupported' if i % 2 else 'supported'}]}],
                                           'usage': None, 'error': None},
                          'key_facts': {'verdicts': [{'index': 0, 'fact': 'f', 'verdict': 'present'}], 'usage': None, 'error': None}}
        items.append({'set': 'questions.yaml', 'id': f'Q{i}', 'type': 'answerable', 'question': f'問題{i}', 'key_facts': ['f'],
                      'retrieved_contexts': [{'chunk_id': 'MFG-SOP-001#1', 'text': '第一行\n第二行'}], 'models': {spec: entry}})
    path = write_labels({'answer_models': ['anthropic:claude-haiku-5-5', 'openai_compatible:gpt-6-luna'], 'items': items},
                        tmp_path, 'stamp')
    sheet = path.read_text(encoding='utf-8')
    for word in ('claude', 'haiku', 'gpt', 'luna', 'anthropic', 'openai', 'unsupported', 'verdict', 'present'):
        assert word not in sheet.split('\n', 3)[3].lower()  # the header lines explain the labels; the data must not
    assert '第一行\n' in sheet  # chunk text stays readable
    filled = sheet.replace("label: ''", 'label: supported')
    path.write_text(filled, encoding='utf-8')
    result = calibrate(path)
    assert result['要點']['labelled'] == 6 and result['要點']['agreement'] == 0.5 and result['要點']['judge_stricter'] == 3
