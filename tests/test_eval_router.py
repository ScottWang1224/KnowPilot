"""Router evaluation harness (scripts/eval_router.py) with the fake provider: no network, no API key."""
import json


def make_client(script):
    from llm.client import LLM
    from llm.guard import ConcurrencyGate, DailyBudget
    from llm.pricing import Pricing
    from llm.providers.fake import FakeProvider
    provider = FakeProvider(script)
    return LLM(provider, 'fake:eval', purpose='router', pricing=Pricing({}), budget=DailyBudget(limit_usd=100),
               gate=ConcurrencyGate(limit=5)), provider


def clarify(dimension, options=('A', 'B'), safety_note=None):
    return json.dumps({'action': 'clarify', 'state': {}, 'selected': [], 'rewritten_queries': [],
                       'clarify': {'dimension': dimension, 'question': '哪一個？', 'options': list(options),
                                   'safety_note': safety_note}}, ensure_ascii=False)


def answer(*codes, multiple=False):
    return json.dumps({'action': 'answer', 'state': {}, 'rewritten_queries': [], 'multiple_possibilities': multiple,
                       'selected': [{'doc_code': c, 'sections': []} for c in codes]})


def test_scenario_replies_by_dimension_and_scores_completion_safety_and_leaks(seeded):
    from scripts.eval_router import evaluate_scenarios
    db, departments = seeded
    client, provider = make_client([clarify('phenomenon', safety_note='先通報班長'), clarify('pattern'),
                                    answer('MFG-SOP-009', 'RD-RPT-001')])
    scenario = {'id': 'S99', 'asker_department': 'MFG', 'opening': '膜怪怪的', 'replies': {'phenomenon': '表面有線條'},
                'expected_docs': ['MFG-SOP-009', 'MFG-SOP-011'], 'must_not_docs': ['RD-RPT-001'],
                'max_turns': 3, 'expect_safety_note': True}
    row = evaluate_scenarios(db, [scenario], client, departments)[0]
    replies = [call['messages'][-1]['content'] for call in provider.calls[1:]]
    assert '<question>表面有線條</question>' in replies[0] and '<question>不知道</question>' in replies[1]
    assert '1. A\n2. B' in provider.calls[1]['messages'][1]['content']  # history keeps the numbered options
    assert row['completion'] == 0.5 and row['safety_note'] and row['unknown_replies'] == 1
    assert row['leaked'] == []  # RD-RPT-001 was dropped on the server before it could reach the chunks
    assert [t['dropped'] for t in row['turns']][-1] == ['RD-RPT-001']


def test_questions_answer_unknown_until_answer_and_summary_rates(seeded):
    from scripts.eval_router import evaluate_questions, item_ok, summarize
    db, departments = seeded
    questions = [
        {'id': 'Q1', 'type': 'answerable', 'asker_department': 'MFG', 'question': 'UV 燈管多久換？',
         'expected_docs': ['MFG-SOP-001']},
        {'id': 'Q2', 'type': 'clarify', 'asker_department': 'MFG', 'question': '塗布頭怎麼清潔？',
         'expected_docs': ['MFG-SOP-002', 'MFG-SOP-003']},
        {'id': 'Q3', 'type': 'unanswerable', 'asker_department': 'PE', 'question': 'CL-03 開機程序？', 'expected_docs': []},
    ]
    client, provider = make_client([clarify('line'), answer('MFG-SOP-001'),
                                    clarify('line'), answer('MFG-SOP-002', 'MFG-SOP-003', multiple=True),
                                    json.dumps({'action': 'out_of_scope'})])
    rows = evaluate_questions(db, questions, client, departments, {'Q1': {'recall@10': 1.0}})
    assert '<question>不知道</question>' in provider.calls[1]['messages'][-1]['content']
    assert [r['first_action'] for r in rows] == ['clarify', 'clarify', 'out_of_scope']
    assert rows[0]['hit'] == 'expected' and rows[1]['all_versions'] and rows[2]['out_of_scope']
    assert rows[0]['bm25_recall@10'] == 1.0 and all(item_ok(r) for r in rows)
    summary = summarize(rows)
    assert summary['over_clarify'] == 1.0 and summary['clarify_detection'] == 1.0
    assert summary['calls'] == 5 and summary['total_cost_usd'] is None and summary['total_budget_cost_usd'] > 0


def test_unauthorized_counts_documents_selected_in_any_turn(seeded):
    from scripts.eval_router import evaluate_questions, seen_docs
    db, departments = seeded
    assert seen_docs([{'selected': ['RD-RULE-001']}], ['MFG-SOP-001']) == {'RD-RULE-001', 'MFG-SOP-001'}
    client, _ = make_client([answer('RD-RULE-001')])
    q = {'id': 'Q4', 'type': 'unauthorized', 'asker_department': 'MFG', 'question': 'RF-25 配方觸媒放多少？',
         'expected_docs': ['RD-RULE-001']}
    row = evaluate_questions(db, [q], client, departments, {})[0]
    assert row['passed'] and row['turns'][0]['dropped'] == ['RD-RULE-001']


def test_followups_answer_earlier_turns_and_score_only_the_last_one(seeded):
    from llm.client import LLM
    from llm.guard import ConcurrencyGate, DailyBudget
    from llm.pricing import Pricing
    from llm.providers.fake import FakeProvider
    from rag import index
    from scripts.eval_router import evaluate_followups, item_ok, summarize
    db, departments = seeded
    first = next(c for c in index.get_index(db).chunks if c.doc_code == 'MFG-SOP-002')
    with_state = json.loads(answer('MFG-SOP-003'))
    with_state.update(state={'line': 'CL-02', 'topic': '塗布頭清潔'}, rewritten_queries=['CL-02 塗布頭 清潔'])
    client, provider = make_client([clarify('line'), answer('MFG-SOP-002'), json.dumps(with_state),
                                    answer('MFG-SOP-002', 'RD-RPT-001')])
    answer_provider = FakeProvider([json.dumps({'status': 'answered', 'points': [
        {'text': '依 SOP 清潔', 'citations': [first.chunk_id], 'condition': 'CL-01'}]}, ensure_ascii=False)])
    answer_client = LLM(answer_provider, 'fake:answer', purpose='answer', pricing=Pricing({}),
                        budget=DailyBudget(limit_usd=100), gate=ConcurrencyGate(limit=5))
    items = [{'id': 'F1', 'asker_department': 'MFG', 'turns': ['塗布頭怎麼清潔？', '那 CL-02 呢？'],
              'expected_docs': ['MFG-SOP-003'], 'must_not_clarify': True, 'must_not_carry': ['CL-01']},
             {'id': 'F2', 'asker_department': 'MFG', 'turns': ['研發的配方分析呢？'], 'expected_docs': [],
              'must_not_clarify': False, 'must_not_docs': ['RD-RPT-001']}]
    rows = evaluate_followups(db, items, client, answer_client, departments)
    # Earlier turn: clarify → 「不知道」 → answer; the answer's compact text is in the last turn's history.
    assert '<question>不知道</question>' in provider.calls[1]['messages'][-1]['content']
    last = provider.calls[2]['messages']
    assert [m['content'] for m in last[-3:-1]] == ['不知道', '- （CL-01）依 SOP 清潔']
    assert rows[0]['recall'] == 1 and rows[0]['clarified'] is False and rows[0]['carried'] == [] and item_ok(rows[0])
    assert rows[1]['leaked'] == [] and rows[1]['turns'][-1]['dropped'] == ['RD-RPT-001'] and item_ok(rows[1])
    assert summarize(rows)['followup_passed'] == 1.0 and summarize(rows)['followup_answer_budget_cost_usd'] > 0


def test_carried_checks_state_values_and_queries():
    from scripts.eval_router import carried
    record = {'state': {'line': 'CL-02', 'topic': '訪客'}, 'queries': ['訪客 入廠 凹版輥']}
    assert carried(record, ['CL-02', '凹版輥', 'HC-350']) == ['CL-02', '凹版輥']


def test_budget_exhaustion_stops_the_run_instead_of_scoring_fallbacks(seeded, monkeypatch):
    from llm.guard import DailyBudget
    from scripts import eval_router
    db, _ = seeded
    client, provider = make_client([answer('MFG-SOP-001')])
    client.budget = DailyBudget(limit_usd=0)
    monkeypatch.setattr(eval_router, 'create_client', lambda spec, purpose, budget: client)
    questions = {'questions.yaml': [{'id': 'Q1', 'type': 'answerable', 'asker_department': 'MFG',
                                     'question': 'UV 燈管多久換？', 'expected_docs': ['MFG-SOP-001']}]}
    report = eval_router.run_models(db, ['fake:eval'], questions, [], {})
    assert report['aborted'] and report['models']['fake:eval'] == []


def test_any_memory_proposal_counts_as_misproposal_and_remember_fails_the_item(seeded):
    from scripts.eval_router import evaluate_questions, item_ok, summarize
    db, departments = seeded
    questions = [{'id': f'Q{n}', 'type': 'answerable', 'asker_department': 'MFG', 'question': 'CL-01 UV 燈管多久換？',
                  'expected_docs': ['MFG-SOP-001']} for n in (1, 2, 3)]
    proposal = [{'key': 'line', 'value': 'CL-01'}]
    client, _ = make_client([answer('MFG-SOP-001'),
                             json.dumps({**json.loads(answer('MFG-SOP-001')), 'memory_proposal': proposal}),
                             json.dumps({'action': 'remember', 'memory_proposal': proposal})])
    rows = evaluate_questions(db, questions, client, departments, {})
    assert [item_ok(r) for r in rows] == [True, True, False]
    assert summarize(rows)['memory_misproposal'] == round(2 / 3, 3)
