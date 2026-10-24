"""Memory evaluation (phase 5 spec 7): python -m scripts.eval_memory [--router-model A] [--answer-model B].

Each item gets a fresh user of its department in a temporary SQLite database, with the item's
memory written through repositories.memories and read back like the chat API does
(validated against the user's options). Then the question is routed with that memory; a
clarifying question is answered 「不知道」. Only style items call the answer model. Every
check is deterministic. LLM calls go through the llm/ layer but are not written to llm_calls.
"""
import argparse
import json
import sys
import unicodedata
from datetime import datetime
from pathlib import Path

import yaml
from dotenv import load_dotenv

from core.config import ROOT
from llm import settings
from llm.client import create_client
from llm.guard import DailyBudget
from llm.pricing import Pricing
from rag import answer as answers, memory, router
from rag.pipeline import Conversation, run_turn
from repositories import memories, users
from routers.memories import user_memory
from scripts.eval_router import (EST_INPUT_TOKENS, EST_OUTPUT_TOKENS, MAX_FOLLOWUPS, UNKNOWN, BudgetExhausted,
                                 item_usage, run_dialogue, turn_record, unique_docs)
from scripts.local_db import open_database

MEMORY_FILE = ROOT/'data'/'eval'/'memory.yaml'
TYPES = ('fill', 'override', 'proposal', 'no_proposal', 'invalid', 'no_access', 'style')
MUST_PASS = ('invalid', 'no_access')            # spec 9.3: these must all pass
EST_ROUTER_CALLS_PER_ITEM, EST_ANSWER_TOKENS = 1.5, (5_000, 900)


def nfkc(text):
    return unicodedata.normalize('NFKC', str(text)).strip()


def make_user(db, department_id, n, memory_items=None):
    """A fresh user of the department with memory_items stored like the profile page does (also used by
    scripts.eval_security). Read the memory back with routers.memories.user_memory, as the chat API does."""
    stamp = datetime.now().strftime('%H%M%S%f')
    user_id = users.create(db, email=f'memory-eval-{stamp}-{n}@example.com', name=f'記憶評測{n}', password_hash='x',
                           department_id=department_id)
    for key, value in (memory_items or {}).items():
        memories.set_value(db, user_id, key, value, 'manual')
    return users.by_id(db, user_id)


def visible_texts(records, route, answer_result):
    """What the user would read: clarifying questions, the memory card, the answer."""
    texts = [t['clarify_text'] for t in records if t['clarify_text']]
    if route.action == 'remember':
        items = route.decision.memory_proposal if route.decision else []
        texts.append('、'.join(f'{memory.LABELS[i.key]} {i.value}' for i in items) or memory.NOTHING_TO_REMEMBER)
    if answer_result is not None and answer_result.output is not None:
        out = answer_result.output
        texts += [p.text + (p.condition or '') for p in out.points] + [out.missing or '']
        texts.append(out.safety_note.text if out.safety_note else '')
    return [t for t in texts if t]


def run_item(db, item, user, router_client, answer_client):
    """Route with the user's memory as the chat API does; style items are also answered with their preferences.

    Returns (turn records, final RouteResult, AnswerResult or None, the Memory sent)."""
    remembered = user_memory(db, user)
    if item['type'] != 'style':
        records, route = run_dialogue(db, user, item['question'], router_client, lambda _: UNKNOWN,
                                      memory=remembered.context)
        return records, route, None, remembered
    conv, text, records = Conversation(), item['question'], []
    while True:
        turn = run_turn(db, user, text, conv, router_client=router_client, answer_client=answer_client,
                        memory=remembered)
        records.append(turn_record(text, turn.route))
        if turn.route.error == 'budget_exceeded' or (turn.answer and turn.answer.error == 'budget_exceeded'):
            raise BudgetExhausted()
        if turn.route.action != 'clarify' or len(records) > MAX_FOLLOWUPS:
            return records, turn.route, turn.answer, remembered
        text = UNKNOWN


def check(item, records, route, answer_result, stored_after):
    """({check name: bool} deciding the pass, {name: bool} only reported) for the item's type.

    invalid passes on safety alone (no valid proposal, nothing written, nothing leaked); whether the
    router also chose `remember` with the fixed reply is reported but does not fail the item.
    """
    kind, expect = item['type'], item.get('expect') or {}
    reported = {}
    final_docs = unique_docs(route.chunks)
    seen = {d for t in records for d in t['selected']} | set(final_docs)
    first = records[0]
    proposal = [(p['key'], p['value']) for p in first['memory_proposal']]
    checks = {}
    if item.get('expected_docs'):
        checks['expected_docs'] = all(d in final_docs for d in item['expected_docs'])
    if item.get('must_not_docs'):
        checks['must_not_docs'] = not (seen & set(item['must_not_docs']))
    if item.get('must_not_reveal'):
        text = nfkc('\n'.join(visible_texts(records, route, answer_result)))
        checks['must_not_reveal'] = not any(nfkc(s) in text for s in item['must_not_reveal'])
    if kind == 'fill':
        checks['no_clarify'] = first['action'] != 'clarify'
        checks['used_memory'] = all(k in first['used_memory'] for k in expect.get('used_memory', []))
    elif kind == 'override':
        # The asked value is in the state and the remembered one is not (e.g. "CL-01（狹縫塗布）" passes).
        state, remembered = route.decision.state if route.decision else {}, item.get('memory') or {}
        checks['state'] = all(nfkc(v) in nfkc(state.get(k, '')) and
                              (k not in remembered or nfkc(remembered[k]) not in nfkc(state.get(k, '')))
                              for k, v in expect.get('state', {}).items())
    elif kind == 'proposal':
        checks['proposal'] = sorted(proposal) == sorted((p['key'], p['value']) for p in expect['proposal'])
        checks['remember'] = (first['action'] == 'remember') == bool(expect.get('remember'))
    elif kind == 'no_proposal':
        checks['no_proposal'] = not any(t['memory_proposed'] for t in records)
    elif kind == 'invalid':
        checks['no_valid_proposal'] = not any(t['memory_proposal'] for t in records)
        checks['not_written'] = stored_after == {str(k): str(v) for k, v in (item.get('memory') or {}).items()}
        reported['fixed_reply'] = first['action'] == 'remember' and not proposal
    elif kind == 'style':
        out = answer_result.output if answer_result else None
        checks['answered'] = out is not None and out.status != 'not_found'
        checks['max_points'] = out is not None and len(out.points) <= expect.get('max_points', 8)
        if expect.get('safety_note'):
            checks['safety_note'] = out is not None and out.safety_note is not None
    return checks, reported


def evaluate(db, items, router_client, answer_client):
    departments = {r['code']: r['id'] for r in db.query('SELECT id,code FROM departments')}
    rows = []
    for n, item in enumerate(items, 1):
        user = make_user(db, departments[item['asker_department']], n, item.get('memory'))
        records, route, answer_result, remembered = run_item(db, item, user, router_client, answer_client)
        checks, reported = check(item, records, route, answer_result, memories.values(db, user['id']))
        usage = item_usage(records)
        if answer_result is not None and answer_result.llm is not None:
            usage = {**usage, 'answer_budget_cost_usd': answer_result.llm.budget_cost_usd,
                     'answer_cost_usd': answer_result.llm.cost_usd}
        out = answer_result.output if answer_result else None
        rows.append({'id': item['id'], 'type': item['type'], 'asker': item['asker_department'],
                     'question': item['question'], 'memory': item.get('memory') or {},
                     'memory_sent': remembered.items(), 'passed': all(checks.values()), 'checks': checks, 'reported': reported,
                     'first_action': records[0]['action'], 'final_action': route.action,
                     'final_docs': unique_docs(route.chunks),
                     'state': route.decision.state if route.decision else {},
                     'answer': None if out is None else {'status': out.status, 'points': [p.text for p in out.points],
                                                         'safety_note': out.safety_note.text if out.safety_note else None},
                     'turns': records, 'usage': usage})
        print(f'  {item["id"]} {"✔" if rows[-1]["passed"] else "✘"}', flush=True)
    return rows


def summarize(rows):
    by_type = {}
    for kind in TYPES:
        group = [r for r in rows if r['type'] == kind]
        if group:
            by_type[kind] = {'passed': sum(r['passed'] for r in group), 'items': len(group)}
    budget = sum(r['usage']['budget_cost_usd'] + r['usage'].get('answer_budget_cost_usd', 0) for r in rows)
    costs = [r['usage']['cost_usd'] for r in rows] + [r['usage']['answer_cost_usd'] for r in rows
                                                      if 'answer_cost_usd' in r['usage']]
    invalid = [r for r in rows if r['type'] == 'invalid']
    return {'by_type': by_type, 'passed': sum(r['passed'] for r in rows), 'items': len(rows),
            'invalid_fixed_reply': {'yes': sum(r['reported']['fixed_reply'] for r in invalid), 'items': len(invalid)},
            'must_pass_ok': all(r['passed'] for r in rows if r['type'] in MUST_PASS),
            'total_cost_usd': None if any(c is None for c in costs) else round(sum(costs), 6),
            'total_budget_cost_usd': round(budget, 6)}


def print_report(report):
    s = report['summary']
    print(f'\n===== 記憶評測 路由 {report["router_model"]}（{report["router_prompt_hash"]}）'
          f'／回答 {report["answer_model"]}（{report["answer_prompt_hash"]}）=====')
    if report['aborted']:
        print(f'!! {report["aborted"]}')
    for kind, r in s['by_type'].items():
        must = '（必須全部通過）' if kind in MUST_PASS else ''
        print(f'  {kind:<12} {r["passed"]}/{r["items"]}{must}')
    fixed = s['invalid_fixed_reply']
    if fixed['items']:
        print(f'  （只列入報告）invalid 以 remember＋固定說明回覆：{fixed["yes"]}/{fixed["items"]}')
    print(f'  合計 {s["passed"]}/{s["items"]}；invalid／no_access {"全部通過" if s["must_pass_ok"] else "未全部通過"}')
    cost = '無價格資料' if s['total_cost_usd'] is None else f'${s["total_cost_usd"]:.4f}'
    print(f'  實際成本 {cost}；預算計入 ${s["total_budget_cost_usd"]:.4f}')
    failed = [r for r in report['rows'] if not r['passed']]
    if failed:
        print('\n未通過的題目：')
        for r in failed:
            bad = ', '.join(k for k, ok in r['checks'].items() if not ok)
            print(f'  {r["id"]}（{r["type"]}）未通過：{bad}；第一輪 {r["first_action"]} → {r["final_action"]} '
                  f'{r["final_docs"][:5]}；state {json.dumps(r["state"], ensure_ascii=False)}；'
                  f'提議 {r["turns"][0]["memory_proposal"]}（丟棄 {r["turns"][0]["dropped_memory"]}）')
            if r['answer']:
                print(f'      回答 {r["answer"]["status"]}：{len(r["answer"]["points"])} 點；'
                      f'安全提示 {"有" if r["answer"]["safety_note"] else "無"}')


def estimate(items, router_spec, answer_spec):
    """(router calls, answer calls, actual or None, budget): a rough upper bound printed before running."""
    pricing = Pricing()
    calls = round(len(items) * EST_ROUTER_CALLS_PER_ITEM)
    n_answers = sum(item['type'] == 'style' for item in items)
    parts = [(router_spec, calls * EST_INPUT_TOKENS, calls * EST_OUTPUT_TOKENS),
             (answer_spec, n_answers * EST_ANSWER_TOKENS[0], n_answers * EST_ANSWER_TOKENS[1])]
    actual = [pricing.actual_cost(spec, i, o) for spec, i, o in parts]
    budget = sum(pricing.budget_cost(spec, i, o) for spec, i, o in parts)
    return calls, n_answers, None if any(a is None for a in actual) else sum(actual), budget


def main():
    sys.stdout.reconfigure(encoding='utf-8')
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--router-model', metavar='PROVIDER:MODEL', help='overrides LLM_ROUTER_MODEL')
    parser.add_argument('--answer-model', metavar='PROVIDER:MODEL', help='for style items; overrides LLM_ANSWER_MODEL')
    parser.add_argument('--items', default=str(MEMORY_FILE), help='memory question file (default: data/eval/memory.yaml)')
    parser.add_argument('--budget-usd', type=float, help='budget for this run, overriding LLM_DAILY_BUDGET_USD')
    parser.add_argument('--estimate-only', action='store_true', help='print the estimated LLM cost and exit')
    args = parser.parse_args()
    load_dotenv(ROOT/'.env')   # API keys and LLM limits; the database is always a temporary one
    router_spec = args.router_model or settings.router_model()
    answer_spec = args.answer_model or settings.answer_model()
    if not router_spec or not answer_spec:
        parser.error('需要路由與回答模型：請在 .env 設定 LLM_ROUTER_MODEL、LLM_ANSWER_MODEL，或使用參數指定')
    items = yaml.safe_load(Path(args.items).read_text(encoding='utf-8')) or []
    calls, n_answers, actual, budget_cost = estimate(items, router_spec, answer_spec)
    print(f'記憶評測 {len(items)} 題：預估路由 {calls} 次（{router_spec}）、回答 {n_answers} 次（{answer_spec}）')
    print(f'  預估實際成本 {"無價格資料" if actual is None else f"${actual:.4f}"}；預算計入 ${budget_cost:.4f}（粗估上限）')
    if args.estimate_only:
        return 0
    budget = DailyBudget(limit_usd=args.budget_usd) if args.budget_usd is not None else None
    router_client = create_client(router_spec, purpose='router', budget=budget)
    answer_client = create_client(answer_spec, purpose='answer', budget=budget)
    report = {'created_at': datetime.now().isoformat(timespec='seconds'), 'router_model': router_spec,
              'answer_model': answer_spec, 'router_prompt_hash': router.prompt_version()[1],
              'answer_prompt_hash': answers.prompt_version()[1],
              'aborted': None, 'rows': []}
    with open_database() as (db, _):
        try:
            report['rows'] = evaluate(db, items, router_client, answer_client)
        except BudgetExhausted:
            report['aborted'] = '預算用完，評測中止'
    report['summary'] = summarize(report['rows'])
    print_report(report)
    out = ROOT/'.eval-runs'
    out.mkdir(exist_ok=True)
    path = out/f'{datetime.now():%Y%m%d-%H%M%S}_memory.json'
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding='utf-8')
    print(f'\n結果已存到 {path.relative_to(ROOT)}')
    return 0 if report['summary']['must_pass_ok'] and not report['aborted'] else 1


if __name__ == '__main__':
    sys.exit(main())
