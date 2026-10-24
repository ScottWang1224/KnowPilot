"""Router evaluation (spec 3.9), driven by scripts.eval_retrieval --router-model.

Every question is routed as a dialogue: whenever the router asks back, the simulated
user replies. Single questions always reply 「不知道」, so the router ends with an
answer within three follow-ups; scenarios reply from their `replies` by dimension.
Recall here is measured on the final chunks (the set the answer model would get),
not on a ranking, and compared with the phase 1 BM25 ranking of the same question.
"""
import math
from statistics import mean

from llm import settings
from llm.client import create_client
from rag import router
from scripts.chat_cli import clarify_text

UNKNOWN = '不知道'
MAX_FOLLOWUPS = 3
EST_INPUT_TOKENS, EST_OUTPUT_TOKENS = 10_000, 700   # per call, from the 2-B smoke tests (rounded up)
EST_CALLS_PER_QUESTION, EST_CALLS_PER_SCENARIO = 1.5, 3


class BudgetExhausted(Exception):
    """The daily budget ran out mid-run; later results would only measure the BM25 fallback."""


def unique_docs(hits):
    return list(dict.fromkeys(h.doc_code for h in hits))


def turn_record(text, result):
    d, llm = result.decision, result.llm
    return {'input': text, 'action': result.action, 'error': result.error, 'fallback': result.fallback,
            'dimension': d.clarify.dimension if d and d.clarify else None,
            'options': d.clarify.options if d and d.clarify else None,
            'clarify_text': clarify_text(d.clarify) if d and d.clarify else None,
            'safety_note': (d.clarify.safety_note or None) if d and d.clarify else None,
            'state': d.state if d else {}, 'queries': d.rewritten_queries if d else [], 'selected': [s.doc_code for s in d.selected] if d else [],
            'multiple_possibilities': bool(d and d.multiple_possibilities), 'dropped': result.dropped_docs,
            # The question sets never ask to remember anything; any proposal (kept or dropped) is a mistake.
            'memory_proposed': result.action == 'remember' or bool(d and d.memory_proposal) or result.dropped_memory > 0,
            'used_memory': d.used_memory if d else [],
            'memory_proposal': [p.model_dump() for p in d.memory_proposal] if d else [],
            'dropped_memory': result.dropped_memory,
            'chunks': [h.chunk_id for h in result.chunks],
            'llm': None if llm is None else {
                'input_tokens': llm.input_tokens, 'output_tokens': llm.output_tokens, 'latency_ms': llm.latency_ms,
                'cost_usd': llm.cost_usd, 'budget_cost_usd': llm.budget_cost_usd, 'attempts': llm.attempts,
                'validation_failed': llm.validation_failed, 'error_detail': llm.error_detail}}


def run_dialogue(db, user, opening, client, reply_for, *, use_glossary=True, history=None, memory=None):
    """Route the opening and answer each clarifying question. Returns (turn records, final RouteResult).

    history: earlier answered exchanges (follow-up questions); state and clarify turns start empty.
    memory: the user's work context, as the chat API passes it (scripts.eval_memory); None elsewhere."""
    history, state, turns, text = list(history or []), None, 0, opening
    records = []
    while True:
        result = router.route(db, user, text, history=history, state=state, clarify_turns=turns,
                              client=client, use_glossary=use_glossary, memory=memory)
        records.append(turn_record(text, result))
        if result.error == 'budget_exceeded':
            raise BudgetExhausted()
        if result.action != 'clarify' or turns >= MAX_FOLLOWUPS:
            return records, result
        history += [{'role': 'user', 'content': text},
                    {'role': 'assistant', 'content': clarify_text(result.decision.clarify)}]
        state, turns = result.decision.state, turns + 1
        text = reply_for(result.decision.clarify.dimension)


def item_usage(records):
    """Token, latency and cost totals of one question or scenario (all of its turns)."""
    calls = [r['llm'] for r in records if r['llm']]
    costs = [c['cost_usd'] for c in calls]
    return {'calls': len(calls), 'input_tokens': sum(c['input_tokens'] for c in calls),
            'output_tokens': sum(c['output_tokens'] for c in calls), 'latency_ms': sum(c['latency_ms'] for c in calls),
            'cost_usd': None if any(c is None for c in costs) else sum(costs),
            'budget_cost_usd': sum(c['budget_cost_usd'] for c in calls)}


def seen_docs(records, final_docs):
    """Every document the router selected in any turn, plus the final chunks' documents."""
    return set(final_docs) | {code for r in records for code in r['selected']}


def evaluate_questions(db, questions, client, departments, baseline, *, use_glossary=True):
    rows = []
    for q in questions:
        user = {'id': 0, 'department_id': departments[q['asker_department']]}
        records, final = run_dialogue(db, user, q['question'], client, lambda _: UNKNOWN, use_glossary=use_glossary)
        docs = unique_docs(final.chunks)
        expected, acceptable = q.get('expected_docs') or [], q.get('acceptable_docs') or []
        row = {'id': q['id'], 'type': q['type'], 'asker': q['asker_department'], 'question': q['question'],
               'expected': expected, 'acceptable': acceptable, 'final_action': final.action, 'final_docs': docs,
               'first_action': records[0]['action'], 'turns': records, 'usage': item_usage(records)}
        base = baseline.get(q['id'], {})
        row['bm25_recall@10'] = base.get('recall@10')
        if q['type'] == 'answerable':
            row['hit'] = 'expected' if set(docs) & set(expected) else 'acceptable' if set(docs) & set(acceptable) else None
            row['recall'] = 1.0 if row['hit'] else 0.0
        elif q['type'] == 'multi_doc':
            row['recall'] = len(set(docs) & set(expected)) / len(expected)
        elif q['type'] == 'clarify':
            row['recall'] = 1.0 if set(docs) & set(expected) else 0.0
            row['all_versions'] = set(expected) <= set(docs)
        elif q['type'] == 'unauthorized':
            row['leaked'] = sorted(seen_docs(records, docs) & set(expected))
            row['passed'] = not row['leaked']
        elif q['type'] == 'unanswerable':
            row['out_of_scope'] = final.action == 'out_of_scope'
        rows.append(row)
    return rows


def evaluate_scenarios(db, scenarios, client, departments):
    rows = []
    for s in scenarios:
        user = {'id': 0, 'department_id': departments[s['asker_department']]}
        replies = {k: str(v) for k, v in (s.get('replies') or {}).items()}
        records, final = run_dialogue(db, user, s['opening'], client, lambda dim: replies.get(dim, UNKNOWN))
        docs = unique_docs(final.chunks)
        expected = s['expected_docs']
        row = {'id': s['id'], 'type': 'scenario', 'asker': s['asker_department'], 'question': s['opening'],
               'expected': expected, 'acceptable': s.get('acceptable_docs') or [], 'final_action': final.action,
               'final_docs': docs, 'first_action': records[0]['action'], 'turns': records, 'usage': item_usage(records),
               'completion': len(set(docs) & set(expected)) / len(expected),
               'acceptable_found': sorted(set(docs) & set(s.get('acceptable_docs') or [])),
               'leaked': sorted(seen_docs(records, docs) & set(s.get('must_not_docs') or [])),
               'unknown_replies': sum(r['input'] == UNKNOWN for r in records[1:])}
        if s.get('expect_safety_note'):
            row['safety_note'] = any(r['safety_note'] for r in records)
        if s.get('expect_multiple_possibilities'):
            row['multiple_possibilities'] = final.decision is not None and final.decision.multiple_possibilities
        rows.append(row)
    return rows


def carried(record, forbidden):
    """Old conditions found in a router turn's state values or rewritten queries."""
    text = ' '.join([*map(str, (record['state'] or {}).values()), *record['queries']])
    return [word for word in forbidden if word in text]


def evaluate_followups(db, items, client, answer_client, departments):
    """Earlier turns go through the chat pipeline (router + real answer, history kept like the chat API);
    the last turn is routed as a dialogue that replies 「不知道」 and is the one scored."""
    from rag.pipeline import Conversation, run_turn
    rows = []
    for item in items:
        user = {'id': 0, 'department_id': departments[item['asker_department']]}
        conv, earlier, answer_calls = Conversation(), [], []
        for text in item['turns'][:-1]:
            for _ in range(MAX_FOLLOWUPS + 1):
                turn = run_turn(db, user, text, conv, router_client=client, answer_client=answer_client,
                                keep_history_after_answer=True)
                earlier.append(turn_record(text, turn.route))
                if turn.route.error == 'budget_exceeded' or (turn.answer and turn.answer.error == 'budget_exceeded'):
                    raise BudgetExhausted()
                if turn.answer and turn.answer.llm:
                    answer_calls.append(turn.answer.llm)
                if turn.route.action != 'clarify':
                    break
                text = UNKNOWN
        records, final = run_dialogue(db, user, item['turns'][-1], client, lambda _: UNKNOWN, history=conv.history)
        docs = unique_docs(final.chunks)
        expected = item.get('expected_docs') or []
        row = {'id': item['id'], 'type': 'followup', 'asker': item['asker_department'], 'question': ' → '.join(item['turns']),
               'expected': expected, 'acceptable': item.get('acceptable_docs') or [], 'final_action': final.action,
               'final_docs': docs, 'first_action': records[0]['action'], 'turns': earlier + records,
               'scored_turns': len(records), 'history': conv.history, 'usage': item_usage(earlier + records),
               'answer_budget_cost_usd': round(sum(c.budget_cost_usd for c in answer_calls), 6),
               'leaked': sorted((seen_docs(records + earlier, docs)) & set(item.get('must_not_docs') or []))}
        if expected:
            row['recall'] = len(set(docs) & set(expected)) / len(expected)
        if item.get('must_not_clarify'):
            row['clarified'] = records[0]['action'] == 'clarify'
        if item.get('must_not_carry'):
            row['carried'] = carried(records[0], item['must_not_carry'])
        rows.append(row)
    return rows


def item_ok(row):
    """One pass/fail per item, used to compare models question by question."""
    kind = row['type']
    if any(t['action'] == 'remember' for t in row['turns']):
        return False
    if kind == 'followup':
        return (row.get('recall', 1) == 1 and not row.get('clarified') and not row.get('carried')
                and not row['leaked'])
    if kind == 'unauthorized':
        return row['passed']
    if kind == 'unanswerable':
        return row['out_of_scope']
    if kind == 'scenario':
        return row['completion'] == 1 and not row['leaked']
    return row['recall'] == 1


def _rate(values):
    values = list(values)
    return round(sum(values) / len(values), 3) if values else None


def p90(values):
    values = sorted(values)
    return values[max(0, math.ceil(0.9 * len(values)) - 1)] if values else None


def summarize(rows):
    by = lambda t: [r for r in rows if r['type'] == t]
    calls = [t['llm'] for r in rows for t in r['turns'] if t['llm']]
    usage = [r['usage'] for r in rows]
    costs = [u['cost_usd'] for u in usage]
    s = {
        'answerable_recall': _rate(r['recall'] for r in by('answerable')),
        'clarify_recall': _rate(r['recall'] for r in by('clarify')),
        'clarify_all_versions': _rate(r['all_versions'] for r in by('clarify')),
        'multi_doc_recall': _rate(r['recall'] for r in by('multi_doc')),
        'clarify_detection': _rate(r['first_action'] == 'clarify' for r in by('clarify')),
        'over_clarify': _rate(r['first_action'] == 'clarify' for r in by('answerable')),
        'unauthorized_pass': _rate(r['passed'] for r in by('unauthorized')),
        'unanswerable_out_of_scope': _rate(r['out_of_scope'] for r in by('unanswerable')),
        'scenario_completion': _rate(r['completion'] for r in by('scenario')),
        'scenario_full': _rate(r['completion'] == 1 for r in by('scenario')),
        'scenario_must_not_pass': _rate(not r['leaked'] for r in by('scenario')),
        'safety_note': _rate(r['safety_note'] for r in by('scenario') if 'safety_note' in r),
        'multiple_possibilities': _rate(r['multiple_possibilities'] for r in by('scenario') if 'multiple_possibilities' in r),
        'followup_recall': _rate(r['recall'] for r in by('followup') if 'recall' in r),
        'followup_no_clarify': _rate(not r['clarified'] for r in by('followup') if 'clarified' in r),
        'followup_no_carry': _rate(not r['carried'] for r in by('followup') if 'carried' in r),
        'followup_must_not_pass': _rate(not r['leaked'] for r in by('followup')),
        'followup_passed': _rate(item_ok(r) for r in by('followup')),
        'followup_answer_budget_cost_usd': round(sum(r['answer_budget_cost_usd'] for r in by('followup')), 6)
        if by('followup') else None,
        'items': len(rows), 'calls': len(calls),
        'fallbacks': sum(bool(t['fallback']) for r in rows for t in r['turns']),
        'validation_failed_calls': sum(c['validation_failed'] for c in calls),
        'retried_calls': sum(c['attempts'] > 1 for c in calls),
        'dropped_doc_calls': sum(bool(t['dropped']) for r in rows for t in r['turns']),
        'memory_misproposal': _rate(any(t.get('memory_proposed') for t in r['turns']) for r in rows),
        'output_at_limit_calls': sum(c['output_tokens'] >= settings.router_max_tokens() for c in calls),
        'avg_input_tokens_per_call': round(mean(c['input_tokens'] for c in calls)) if calls else None,
        'avg_output_tokens_per_call': round(mean(c['output_tokens'] for c in calls)) if calls else None,
        'avg_tokens_per_item': round(mean(u['input_tokens'] + u['output_tokens'] for u in usage)) if usage else None,
        'avg_latency_ms_per_item': round(mean(u['latency_ms'] for u in usage)) if usage else None,
        'p90_latency_ms_per_item': p90([u['latency_ms'] for u in usage]),
        'avg_cost_usd_per_item': None if any(c is None for c in costs) or not costs else round(mean(costs), 6),
        'total_cost_usd': None if any(c is None for c in costs) else round(sum(costs), 6),
        'total_budget_cost_usd': round(sum(u['budget_cost_usd'] for u in usage), 6),
    }
    return s


def estimate_cost(pricing, specs, n_questions, n_scenarios, repeat, n_glossary_questions, n_followup_turns=0):
    """(estimated calls per model, {spec: (actual or None, budget)}) — a rough upper bound shown before running.

    Follow-up turns are counted as router calls; the earlier turns' answers are estimated separately."""
    calls = (n_questions * EST_CALLS_PER_QUESTION + n_scenarios * EST_CALLS_PER_SCENARIO
             + n_glossary_questions * EST_CALLS_PER_QUESTION + n_followup_turns * EST_CALLS_PER_QUESTION) * repeat
    tokens_in, tokens_out = calls * EST_INPUT_TOKENS, calls * EST_OUTPUT_TOKENS
    return calls, {spec: (pricing.actual_cost(spec, tokens_in, tokens_out), pricing.budget_cost(spec, tokens_in, tokens_out))
                   for spec in specs}


# ---- multi-model runs and report ----------------------------------------------------------

SET_METRICS = [  # (summary key, label, kind) kind: rate | count | tokens | ms | usd
    ('answerable_recall', '路由後 Recall：answerable', 'rate'),
    ('clarify_recall', '路由後 Recall：clarify（任一版本）', 'rate'),
    ('clarify_all_versions', '路由後版本涵蓋率：clarify', 'rate'),
    ('multi_doc_recall', '路由後 Recall：multi_doc', 'rate'),
    ('clarify_detection', '追問偵測率（clarify 題第一輪追問）', 'rate'),
    ('over_clarify', '多餘追問率（answerable 第一輪追問，越低越好）', 'rate'),
    ('unauthorized_pass', '權限檢查：unauthorized（必須 100%）', 'rate'),
    ('unanswerable_out_of_scope', 'unanswerable 判為 out_of_scope', 'rate'),
    ('scenario_completion', '情境完成率（expected_docs 涵蓋比例）', 'rate'),
    ('scenario_full', '情境全部涵蓋的比例', 'rate'),
    ('scenario_must_not_pass', '情境權限檢查 must_not_docs（必須 100%）', 'rate'),
    ('safety_note', '安全提示（expect_safety_note 的情境）', 'rate'),
    ('multiple_possibilities', '全程不知道時以多種可能回答', 'rate'),
    ('followup_recall', '延伸題 Recall（最後一句的 expected_docs）', 'rate'),
    ('followup_no_clarify', '延伸題未多餘追問（must_not_clarify）', 'rate'),
    ('followup_no_carry', '換主題未沿用舊條件（must_not_carry）', 'rate'),
    ('followup_must_not_pass', '延伸題權限檢查 must_not_docs（必須 100%）', 'rate'),
    ('followup_passed', '延伸題全部條件通過的比例', 'rate'),
    ('followup_answer_budget_cost_usd', '延伸題前句回答的預算計入成本', 'usd'),
    ('calls', 'LLM 呼叫次數', 'count'),
    ('fallbacks', '保底（LLM 失敗改用 BM25）次數', 'count'),
    ('validation_failed_calls', 'JSON 驗證失敗的呼叫', 'count'),
    ('output_at_limit_calls', '輸出達 max_tokens 上限的呼叫', 'count'),
    ('dropped_doc_calls', '選到不可見文件而被丟棄的呼叫', 'count'),
    ('memory_misproposal', '誤提記憶率（提議記住或 remember，必須為 0）', 'rate'),
    ('avg_input_tokens_per_call', '平均 input token／呼叫', 'tokens'),
    ('avg_output_tokens_per_call', '平均 output token／呼叫', 'tokens'),
    ('avg_tokens_per_item', '平均 token／題', 'tokens'),
    ('avg_latency_ms_per_item', '平均延遲／題（ms）', 'ms'),
    ('p90_latency_ms_per_item', 'P90 延遲／題（ms）', 'ms'),
    ('avg_cost_usd_per_item', '平均實際成本／題', 'usd'),
    ('total_cost_usd', '總實際成本', 'usd'),
    ('total_budget_cost_usd', '總預算計入成本', 'usd'),
]
NO_GLOSSARY = 'paraphrase.yaml（無術語表）'


def run_models(db, specs, question_sets, scenarios, baselines, *, repeat=1, budget=None, no_glossary=False,
               followups=(), answer_spec=None):
    """Run every model `repeat` times over all question sets and scenarios. Stops cleanly when the budget runs out.

    followups are run only with answer_spec, which answers their earlier turns for every router model."""
    departments = {r['code']: r['id'] for r in db.query('SELECT id,code FROM departments')}
    report = {'prompt_name': router.PROMPT_FILE.name, 'prompt_hash': router.prompt_version()[1],
              'router_max_tokens': settings.router_max_tokens(), 'repeat': repeat, 'aborted': None, 'models': {},
              'followup_answer_model': answer_spec if followups else None}
    answer_client = create_client(answer_spec, purpose='answer', budget=budget) if followups and answer_spec else None
    for spec in specs:
        client = create_client(spec, purpose='router', budget=budget)
        runs = report['models'][spec] = []
        try:
            for i in range(repeat):
                print(f'  {spec} 第 {i + 1}/{repeat} 次 ...', flush=True)
                run = {}
                for name, questions in question_sets.items():
                    rows = evaluate_questions(db, questions, client, departments, baselines.get(name, {}))
                    run[name] = {'summary': summarize(rows), 'rows': rows}
                if scenarios:
                    rows = evaluate_scenarios(db, scenarios, client, departments)
                    run['scenarios.yaml'] = {'summary': summarize(rows), 'rows': rows}
                if answer_client is not None:
                    rows = evaluate_followups(db, followups, client, answer_client, departments)
                    run['followups.yaml'] = {'summary': summarize(rows), 'rows': rows}
                if no_glossary and 'paraphrase.yaml' in question_sets:
                    rows = evaluate_questions(db, question_sets['paraphrase.yaml'], client, departments,
                                              baselines.get('paraphrase.yaml', {}), use_glossary=False)
                    run[NO_GLOSSARY] = {'summary': summarize(rows), 'rows': rows}
                runs.append(run)
        except BudgetExhausted:
            report['aborted'] = f'{spec} 第 {len(runs) + 1} 次執行時預算用完，該次與之後的結果未納入'
            print(f'  !! {report["aborted"]}')
            break
    return report


def aggregate(runs, set_name, key):
    values = [run[set_name]['summary'][key] for run in runs if set_name in run]
    values = [v for v in values if v is not None]
    if not values:
        return None
    return {'mean': mean(values), 'min': min(values), 'max': max(values), 'n': len(values)}


def fmt(agg, kind):
    if agg is None:
        return '無價格資料' if kind == 'usd' else '—'
    one = {'rate': '{:.3f}', 'count': '{:g}', 'tokens': '{:,.0f}', 'ms': '{:,.0f}', 'usd': '${:.4f}'}[kind].format
    if agg['n'] > 1 and agg['min'] != agg['max']:
        return f'{one(agg["mean"])}（{one(agg["min"])}～{one(agg["max"])}）'
    return one(agg['mean'])


def passes(runs, set_name, item_id):
    """(passed runs, runs) for one item; an item passes for a model when it passes in most runs."""
    oks = [item_ok(next(r for r in run[set_name]['rows'] if r['id'] == item_id)) for run in runs if set_name in run]
    return sum(oks), len(oks)


def _outcome(row):
    return f'第一輪 {row["first_action"]} → {row["final_action"]} {row["final_docs"][:6]}'


def print_report(report):
    models = {spec: runs for spec, runs in report['models'].items() if runs}
    specs = list(models)
    print(f'\n===== 路由評測 {report["prompt_name"]}（{report["prompt_hash"]}）'
          f' max_tokens={report["router_max_tokens"]} repeat={report["repeat"]} =====')
    if report['aborted']:
        print(f'!! {report["aborted"]}')
    if not specs:
        return
    set_names = list(dict.fromkeys(name for runs in models.values() for run in runs for name in run))
    width = max(len(s) for s in specs) + 4
    for name in set_names:
        print(f'\n--- {name} ---')
        print(f'{"指標":<36}' + ''.join(f'{s:>{width}}' for s in specs))
        for key, label, kind in SET_METRICS:
            aggs = [aggregate(models[s], name, key) for s in specs]
            if all(a is None for a in aggs) and (kind == 'rate' or key.startswith('followup_')):
                continue
            print(f'{label:<36}' + ''.join(f'{fmt(a, kind):>{width}}' for a in aggs))
    for name in set_names:
        if name == NO_GLOSSARY:
            continue
        first = {spec: {r['id']: r for r in runs[0][name]['rows']} for spec, runs in models.items() if name in runs[0]}
        ids = list(first[specs[0]]) if specs[0] in first else []
        differ, all_fail = [], []
        for item_id in ids:
            status = {spec: passes(models[spec], name, item_id) for spec in first}
            verdict = {spec: k * 2 > n for spec, (k, n) in status.items()}
            if len(set(verdict.values())) > 1:
                differ.append((item_id, status))
            elif not any(verdict.values()):
                all_fail.append((item_id, status))
        if len(first) > 1:
            print(f'\n{name}：模型結果不同的題目（✔ 通過／✘ 未通過，括號為通過次數）')
            if not differ:
                print('  （無）')
            for item_id, status in differ:
                row = first[specs[0]][item_id]
                print(f'  {item_id} {row["type"]:<12} {row["question"]}\n      預期 {row["expected"]}')
                for spec, (k, n) in status.items():
                    print(f'      {"✔" if k * 2 > n else "✘"} {spec}（{k}/{n}） {_outcome(first[spec][item_id])}')
        print(f'\n{name}：{"所有模型都" if len(first) > 1 else ""}未通過的題目')
        if not all_fail:
            print('  （無）')
        for item_id, status in all_fail:
            row = first[specs[0]][item_id]
            print(f'  {item_id} {row["type"]:<12} {row["question"]}\n      預期 {row["expected"]}')
            for spec in status:
                print(f'      {spec}：{_outcome(first[spec][item_id])}')
    print_paraphrase_change(models)
    print_glossary_effect(models)


def print_paraphrase_change(models):
    name = 'paraphrase.yaml'
    first = {spec: {r['id']: r for r in runs[0][name]['rows']} for spec, runs in models.items() if name in runs[0]}
    if not first:
        return
    rows = next(iter(first.values())).values()
    missed = [r for r in rows if r['type'] in ('answerable', 'clarify', 'multi_doc')
              and r['bm25_recall@10'] is not None and r['bm25_recall@10'] < 1]
    print('\n口語改寫題：階段 1 BM25 前 10 份未完全命中的題目，路由後的結果')
    for r in missed:
        cells = []
        for spec, rows_by_id in first.items():
            k, n = passes(models[spec], name, r['id'])
            cells.append(f'{spec} {"✔" if k * 2 > n else "✘"}（{k}/{n}） recall={rows_by_id[r["id"]]["recall"]:.2f}')
        print(f'  {r["id"]} BM25 R@10={r["bm25_recall@10"]:.2f} → ' + '｜'.join(cells))


def print_glossary_effect(models):
    lines = []
    for spec, runs in models.items():
        if not any(NO_GLOSSARY in run for run in runs):
            continue
        parts = []
        for key, label, kind in (('answerable_recall', 'answerable', 'rate'), ('multi_doc_recall', 'multi_doc', 'rate'),
                                 ('clarify_recall', 'clarify', 'rate'), ('avg_input_tokens_per_call', 'input token／呼叫', 'tokens')):
            parts.append(f'{label} {fmt(aggregate(runs, "paraphrase.yaml", key), kind)} → {fmt(aggregate(runs, NO_GLOSSARY, key), kind)}')
        lines.append(f'  {spec}：' + '；'.join(parts))
    if lines:
        print('\n術語表效果（paraphrase.yaml：有術語表 → 無術語表）')
        print('\n'.join(lines))
