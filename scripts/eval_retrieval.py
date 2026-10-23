"""Run explicitly: python -m scripts.eval_retrieval. Retrieval recall on data/eval/ question sets.

Recall is measured per document: chunk results are de-duplicated by document and the
first k documents are compared with expected_docs. By default questions.yaml and
paraphrase.yaml (if present) are evaluated and reported separately.

Builds a temporary SQLite database from data/ (migrate + seed) unless --use-env-db is
given, so results do not depend on the local development database. Each question's
asker is an in-memory user whose access still goes through document_scope() in SQL.

--router-model provider:model [provider:model ...] also evaluates the LLM router on the
same question sets plus data/eval/scenarios.yaml, one model after another, and prints
the models side by side (see scripts/eval_router.py). This calls the real APIs and is
charged; the estimated cost is printed first and --estimate-only stops there.
"""
import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

import yaml
from dotenv import load_dotenv
from core.config import ROOT
from llm.guard import DailyBudget
from llm.pricing import Pricing
from rag import index
from scripts import eval_router
from scripts.local_db import open_database

KS = (5, 10)
RECALL_TYPES = ('answerable', 'clarify', 'multi_doc')
SEARCH_CHUNKS = 30  # at most 3 chunks per document, so 30 chunks always yield 10 documents when available
QUESTION_SETS = ('questions.yaml', 'paraphrase.yaml')
SCENARIOS = ROOT/'data'/'eval'/'scenarios.yaml'


def ranked_docs(hits):
    """Document codes in the order they first appear in the chunk results."""
    return list(dict.fromkeys(hit.doc_code for hit in hits))


def recall(expected, docs, k, any_hit):
    found = set(docs[:k]) & set(expected)
    if any_hit:
        return 1.0 if found else 0.0
    return len(found) / len(expected)


def hit_source(expected, acceptable, docs, k):
    """Which list produced the hit within the first k documents: 'expected', 'acceptable' or None."""
    if set(docs[:k]) & set(expected):
        return 'expected'
    if set(docs[:k]) & set(acceptable):
        return 'acceptable'
    return None


def evaluate(db, questions):
    departments = {r['code']: r['id'] for r in db.query('SELECT id,code FROM departments')}
    results = []
    for q in questions:
        user = {'id': 0, 'department_id': departments[q['asker_department']]}
        hits = index.search(db, user, q['question'], k=SEARCH_CHUNKS)
        docs = ranked_docs(hits)[:max(KS)]
        row = {'id': q['id'], 'type': q['type'], 'asker': q['asker_department'], 'question': q['question'],
               'expected': q.get('expected_docs') or [], 'acceptable': q.get('acceptable_docs') or [], 'retrieved': docs,
               'chunks': [{'chunk_id': h.chunk_id, 'section': h.section_path, 'score': h.score} for h in hits]}
        if q['type'] in RECALL_TYPES:
            row['ranks'] = {code: docs.index(code) + 1 if code in docs else None for code in row['expected'] + row['acceptable']}
            for k in KS:
                if q['type'] == 'answerable':
                    # Any expected or acceptable document answers the question.
                    row[f'hit@{k}'] = hit_source(row['expected'], row['acceptable'], docs, k)
                    row[f'recall@{k}'] = 1.0 if row[f'hit@{k}'] else 0.0
                else:
                    # multi_doc needs every expected document; clarify needs any. acceptable_docs is report-only.
                    row[f'recall@{k}'] = recall(row['expected'], docs, k, any_hit=q['type'] == 'clarify')
            if q['type'] == 'clarify':
                row['all_versions@10'] = set(row['expected']) <= set(docs)
        elif q['type'] == 'unauthorized':
            row['leaked'] = sorted(set(docs) & set(row['expected']))
            row['passed'] = not row['leaked']
        results.append(row)
    return results


def summarize(results):
    summary = {}
    for qtype in RECALL_TYPES:
        rows = [r for r in results if r['type'] == qtype]
        if rows:
            summary[qtype] = {'count': len(rows), **{f'recall@{k}': round(sum(r[f'recall@{k}'] for r in rows) / len(rows), 3) for k in KS}}
            if qtype == 'answerable':
                summary[qtype]['hits_via_acceptable@10'] = sum(r['hit@10'] == 'acceptable' for r in rows)
            if qtype == 'clarify':
                summary[qtype]['all_versions@10'] = round(sum(r['all_versions@10'] for r in rows) / len(rows), 3)
    rows = [r for r in results if r['type'] == 'unauthorized']
    if rows:
        summary['unauthorized'] = {'count': len(rows), 'pass_rate': round(sum(r['passed'] for r in rows) / len(rows), 3)}
    return summary


def print_report(summary, results):
    print(f'{"題型":<14}{"題數":>5}{"Recall@5":>10}{"Recall@10":>11}')
    for qtype, s in summary.items():
        if qtype == 'unauthorized':
            print(f'{qtype:<14}{s["count"]:>5}{"權限通過率 " + format(s["pass_rate"], ".0%"):>21}')
            continue
        print(f'{qtype:<14}{s["count"]:>5}{s["recall@5"]:>10.3f}{s["recall@10"]:>11.3f}')
        if s.get('hits_via_acceptable@10'):
            print(f'{"":<19}其中 {s["hits_via_acceptable@10"]} 題只靠 acceptable_docs 命中（前 10 份）')
        if 'all_versions@10' in s:
            print(f'{"":<19}版本涵蓋率（預期文件全部在前 10 份）{s["all_versions@10"]:.3f}')
    print('\n每題結果（預期文件：文件排名，— 表示前 10 份內沒有）')
    for r in results:
        if 'ranks' in r:
            ranks = '、'.join(f'{"（可接受）" if c in r["acceptable"] else ""}{c}:{n or "—"}' for c, n in r['ranks'].items())
            via = '  ← 靠 acceptable_docs' if r.get('hit@10') == 'acceptable' else ''
            print(f'  {r["id"]} {r["type"]:<11} {r["asker"]:<4} R@5={r["recall@5"]:.2f} {ranks}{via}')
        elif 'passed' in r:
            print(f'  {r["id"]} {r["type"]:<11} {r["asker"]:<4} {"通過" if r["passed"] else "洩漏 " + ",".join(r["leaked"])}')
    missed = [r for r in results if r.get('recall@10', 1) < 1 or r.get('all_versions@10') is False or r.get('passed') is False]
    print('\n未完全命中（Recall@10 < 1、追問題版本未全部涵蓋）或權限失敗：')
    for r in missed:
        print(f'  {r["id"]} {r["question"]}\n      預期 {r["expected"]}\n      取得 {r["retrieved"][:6]}')


def print_estimate(specs, question_sets, scenarios, args):
    n_questions = sum(len(q) for q in question_sets.values())
    n_glossary = len(question_sets.get('paraphrase.yaml', [])) if args.no_glossary else 0
    calls, costs = eval_router.estimate_cost(Pricing(), specs, n_questions, len(scenarios), args.repeat, n_glossary)
    budget = f'${args.budget_usd}（--budget-usd）' if args.budget_usd is not None else '.env 的 LLM_DAILY_BUDGET_USD'
    print(f'路由評測預估：每個模型約 {calls:.0f} 次呼叫（題目 {n_questions}、情境 {len(scenarios)}、'
          f'術語表對照 {n_glossary}，×{args.repeat} 次），以每次 {eval_router.EST_INPUT_TOKENS:,} input／'
          f'{eval_router.EST_OUTPUT_TOKENS} output token 估算（偏高）；預算上限 {budget}')
    for spec, (actual, charged) in costs.items():
        print(f'  {spec}：實際 {"無價格資料" if actual is None else f"${actual:.3f}"}，預算計入 ${charged:.3f}')


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--use-env-db', action='store_true', help='use the database from .env instead of a fresh temporary one')
    parser.add_argument('--questions', nargs='+', help='question files (default: data/eval/questions.yaml and paraphrase.yaml)')
    parser.add_argument('--router-model', nargs='+', metavar='PROVIDER:MODEL', help='also evaluate the router with these models')
    parser.add_argument('--no-glossary', action='store_true', help='also run paraphrase.yaml without the glossary for comparison')
    parser.add_argument('--budget-usd', type=float, help='budget for this run, overriding LLM_DAILY_BUDGET_USD')
    parser.add_argument('--repeat', type=int, default=1, help='run the router evaluation N times to see how stable it is')
    parser.add_argument('--estimate-only', action='store_true', help='print the estimated router cost and exit')
    args = parser.parse_args()
    files = [Path(f) for f in args.questions] if args.questions else \
        [ROOT/'data'/'eval'/name for name in QUESTION_SETS if (ROOT/'data'/'eval'/name).exists()]
    question_sets = {path.name: yaml.safe_load(path.read_text(encoding='utf-8')) or [] for path in files}
    scenarios = []
    if args.router_model:
        load_dotenv(ROOT/'.env')  # API keys and LLM limits; the database stays the temporary one
        scenarios = yaml.safe_load(SCENARIOS.read_text(encoding='utf-8')) or [] if SCENARIOS.exists() else []
        print_estimate(args.router_model, question_sets, scenarios, args)
        if args.estimate_only:
            return 0
    router_report = None
    with open_database(args.use_env_db) as (db, source):
        runs = {}
        for name, questions in question_sets.items():
            results = evaluate(db, questions)
            runs[name] = {'summary': summarize(results), 'results': results}
        for name, run in runs.items():
            print(f'\n===== {name} =====')
            print_report(run['summary'], run['results'])
        if args.router_model:
            print('\n開始路由評測 ...')
            baselines = {name: {r['id']: r for r in run['results']} for name, run in runs.items()}
            budget = DailyBudget(limit_usd=args.budget_usd) if args.budget_usd is not None else None
            router_report = eval_router.run_models(db, args.router_model, question_sets, scenarios, baselines,
                                                   repeat=args.repeat, budget=budget, no_glossary=args.no_glossary)
    if router_report:
        eval_router.print_report(router_report)
    out = ROOT/'.eval-runs'
    out.mkdir(exist_ok=True)
    path = out/f'{datetime.now():%Y%m%d-%H%M%S}_{"router" if router_report else "retrieval"}.json'
    path.write_text(json.dumps({'created_at': datetime.now().isoformat(timespec='seconds'), 'database': source,
                                'search_chunks': SEARCH_CHUNKS, 'sets': runs, 'router': router_report},
                               ensure_ascii=False, indent=2), encoding='utf-8')
    print(f'\n結果已存到 {path.relative_to(ROOT)}')
    leaked = any(run['summary'].get('unauthorized', {}).get('pass_rate', 1) < 1 for run in runs.values())
    for runs_of_model in (router_report or {}).get('models', {}).values():
        for run in runs_of_model:
            for result in run.values():
                rates = (result['summary']['unauthorized_pass'], result['summary']['scenario_must_not_pass'])
                leaked |= any(rate is not None and rate < 1 for rate in rates)
    return 1 if leaked else 0


if __name__ == '__main__':
    sys.exit(main())
