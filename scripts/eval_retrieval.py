"""Run explicitly: python -m scripts.eval_retrieval. Retrieval recall on data/eval/ question sets.

Recall is measured per document: chunk results are de-duplicated by document and the
first k documents are compared with expected_docs. By default questions.yaml and
paraphrase.yaml (if present) are evaluated and reported separately.

Builds a temporary SQLite database from data/ (migrate + seed) unless --use-env-db is
given, so results do not depend on the local development database. Each question's
asker is an in-memory user whose access still goes through document_scope() in SQL.
"""
import argparse
import json
import sys
import tempfile
from datetime import datetime
from pathlib import Path

import yaml
from dotenv import load_dotenv
from core.config import ROOT
from core.database import Database, db_engine
from rag import index
from scripts.migrate import migrate
from scripts.seed import seed

KS = (5, 10)
RECALL_TYPES = ('answerable', 'clarify', 'multi_doc')
SEARCH_CHUNKS = 30  # at most 3 chunks per document, so 30 chunks always yield 10 documents when available
QUESTION_SETS = ('questions.yaml', 'paraphrase.yaml')


def ranked_docs(hits):
    """Document codes in the order they first appear in the chunk results."""
    return list(dict.fromkeys(hit.doc_code for hit in hits))


def recall(expected, docs, k, any_hit):
    found = set(docs[:k]) & set(expected)
    if any_hit:
        return 1.0 if found else 0.0
    return len(found) / len(expected)


def evaluate(db, questions):
    departments = {r['code']: r['id'] for r in db.query('SELECT id,code FROM departments')}
    results = []
    for q in questions:
        user = {'id': 0, 'department_id': departments[q['asker_department']]}
        hits = index.search(db, user, q['question'], k=SEARCH_CHUNKS)
        docs = ranked_docs(hits)[:max(KS)]
        row = {'id': q['id'], 'type': q['type'], 'asker': q['asker_department'], 'question': q['question'],
               'expected': q.get('expected_docs') or [], 'retrieved': docs,
               'chunks': [{'chunk_id': h.chunk_id, 'section': h.section_path, 'score': h.score} for h in hits]}
        if q['type'] in RECALL_TYPES:
            row['ranks'] = {code: docs.index(code) + 1 if code in docs else None for code in row['expected']}
            for k in KS:
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
        if 'all_versions@10' in s:
            print(f'{"":<19}版本涵蓋率（預期文件全部在前 10 份）{s["all_versions@10"]:.3f}')
    print('\n每題結果（預期文件：文件排名，— 表示前 10 份內沒有）')
    for r in results:
        if 'ranks' in r:
            ranks = '、'.join(f'{c}:{n or "—"}' for c, n in r['ranks'].items())
            print(f'  {r["id"]} {r["type"]:<11} {r["asker"]:<4} R@5={r["recall@5"]:.2f} {ranks}')
        elif 'passed' in r:
            print(f'  {r["id"]} {r["type"]:<11} {r["asker"]:<4} {"通過" if r["passed"] else "洩漏 " + ",".join(r["leaked"])}')
    missed = [r for r in results if r.get('recall@10', 1) < 1 or r.get('all_versions@10') is False or r.get('passed') is False]
    print('\n未完全命中（Recall@10 < 1、追問題版本未全部涵蓋）或權限失敗：')
    for r in missed:
        print(f'  {r["id"]} {r["question"]}\n      預期 {r["expected"]}\n      取得 {r["retrieved"][:6]}')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--use-env-db', action='store_true', help='use the database from .env instead of a fresh temporary one')
    parser.add_argument('--questions', nargs='+', help='question files (default: data/eval/questions.yaml and paraphrase.yaml)')
    args = parser.parse_args()
    files = [Path(f) for f in args.questions] if args.questions else \
        [ROOT/'data'/'eval'/name for name in QUESTION_SETS if (ROOT/'data'/'eval'/name).exists()]
    with tempfile.TemporaryDirectory() as tmp:
        if args.use_env_db:
            load_dotenv(ROOT/'.env')
            db, source = Database.from_env(), db_engine()
        else:
            db, source = Database.sqlite(Path(tmp)/'eval.db'), 'temporary sqlite from data/'
            migrate(db, 'sqlite')
            seed(db)
        runs = {}
        for path in files:
            results = evaluate(db, yaml.safe_load(path.read_text(encoding='utf-8')) or [])
            runs[path.name] = {'summary': summarize(results), 'results': results}
    for name, run in runs.items():
        print(f'\n===== {name} =====')
        print_report(run['summary'], run['results'])
    out = ROOT/'.eval-runs'
    out.mkdir(exist_ok=True)
    path = out/f'{datetime.now():%Y%m%d-%H%M%S}_retrieval.json'
    path.write_text(json.dumps({'created_at': datetime.now().isoformat(timespec='seconds'), 'database': source,
                                'search_chunks': SEARCH_CHUNKS, 'sets': runs}, ensure_ascii=False, indent=2), encoding='utf-8')
    print(f'\n結果已存到 {path.relative_to(ROOT)}')
    leaked = any(run['summary'].get('unauthorized', {}).get('pass_rate', 1) < 1 for run in runs.values())
    return 1 if leaked else 0


if __name__ == '__main__':
    sys.exit(main())
