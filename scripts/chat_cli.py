"""Run explicitly: python -m scripts.chat_cli --department MFG. Multi-turn router + answer prototype.

Shows what the router decides each turn (clarifying question, selected chunks, tokens,
latency, cost) and, when an answer model is set (LLM_ANSWER_MODEL or --answer-model),
the cited answer. Without an answer model only the chunk list is shown, as in phase 2.
Uses a fresh temporary SQLite built from data/ unless --use-env-db is given; LLM
settings and keys come from .env.
Commands: /new starts a new question, /quit exits. Type an option number to pick it.
"""
import argparse
import json
import sys

from dotenv import load_dotenv
from core.config import ROOT
from llm import settings
from llm.client import create_client
from rag.answer import OUT_OF_SCOPE_TEXT, format_answer
from rag.pipeline import Conversation, clarify_text, run_turn  # noqa: F401  clarify_text is imported from here by eval_router
from scripts.local_db import open_database


def department_user(db, code):
    """In-memory user of a department; access still goes through document_scope() in SQL."""
    row = db.query('SELECT id FROM departments WHERE code=%s', (code.upper(),), one=True)
    if not row:
        raise SystemExit(f'找不到部門代碼：{code}')
    return {'id': 0, 'department_id': row['id']}


def show_llm(llm, budget):
    cost = '無價格資料' if llm.cost_usd is None else f'${llm.cost_usd:.6f}'
    print(f'[LLM] {llm.provider}:{llm.model}  tokens in/out {llm.input_tokens}/{llm.output_tokens}  '
          f'嘗試 {llm.attempts} 次{"（曾驗證失敗）" if llm.validation_failed else ""}  延遲 {llm.latency_ms} ms')
    print(f'[成本] 實際 {cost}  預算計入 ${llm.budget_cost_usd:.6f}  今日累計 ${budget.spent:.6f}')


def show(result, client):
    print(f'\n[action] {result.action}' + (f'  [error] {result.error}' if result.error else '')
          + (f'  [fallback] {result.fallback}' if result.fallback else ''))
    if result.message:
        print(f'  {result.message}')
    decision = result.decision
    if decision:
        print(f'[理解] {decision.intent_summary}')
        print(f'[state] {json.dumps(decision.state, ensure_ascii=False)}')
        print(f'[改寫查詢] {"｜".join(decision.rewritten_queries) or "（無）"}')
        print(f'[selected] {", ".join(s.doc_code + (str(s.sections) if s.sections else "") for s in decision.selected) or "（無）"}')
        if decision.multiple_possibilities:
            print('[多種可能] 是')
        if decision.clarify:
            if decision.clarify.safety_note:
                print(f'[安全提示] {decision.clarify.safety_note}')
            print(f'[追問：{decision.clarify.dimension}] {clarify_text(decision.clarify)}')
        if decision.memory_proposal:
            print(f'[提議記住] {", ".join(f"{m.key}={m.value}" for m in decision.memory_proposal)}')
    if result.dropped_docs:
        print(f'[丟棄的文件] {", ".join(result.dropped_docs)}')
    if result.dropped_memory:
        print(f'[丟棄的記憶提議] {result.dropped_memory} 項')
    if result.chunks:
        print('[要送給回答模型的段落]')
        for hit in result.chunks:
            print(f'  {hit.chunk_id:<16} {hit.section_path}')
    if result.llm:
        show_llm(result.llm, client.budget)


def show_answer(turn, client):
    route, result = turn.route, turn.answer
    if route.action == 'out_of_scope':
        print(f'\n[回答] {OUT_OF_SCOPE_TEXT}')
        return
    if result is None or result.status == 'skipped':
        return
    print(f'\n[回答 status] {result.status}' + (f'  [error] {result.error}' if result.error else ''))
    if result.error in ('budget_exceeded', 'busy'):
        print('  ' + ('今日體驗額度已用完' if result.error == 'budget_exceeded' else '系統忙碌，請稍後再試')
              + '；相關文件：' + '、'.join(dict.fromkeys(h.doc_code for h in route.chunks)))
    if result.output:
        print(format_answer(result.output))
    if result.removed_citations or result.dropped_points:
        print(f'[移除的引用] {", ".join(result.removed_citations) or "（無）"}  [刪除的要點] {result.dropped_points}')
    if result.llm:
        show_llm(result.llm, client.budget)
    costs = [c.llm.cost_usd for c in (route, result) if c.llm]
    if costs:
        total = '無價格資料' if any(c is None for c in costs) else f'${sum(costs):.6f}'
        print(f'[本輪合計] 路由＋回答 {total}')


def main():
    sys.stdout.reconfigure(encoding='utf-8')
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--department', required=True, help='department code, e.g. MFG, PE, QA, RD, ADM')
    parser.add_argument('--router-model', help='provider:model-id, overrides LLM_ROUTER_MODEL')
    parser.add_argument('--answer-model', help='provider:model-id, overrides LLM_ANSWER_MODEL')
    parser.add_argument('--use-env-db', action='store_true', help='use the database from .env instead of a fresh temporary one')
    args = parser.parse_args()
    load_dotenv(ROOT/'.env')
    spec = args.router_model or settings.router_model()
    if not spec:
        raise SystemExit('未設定路由模型：請在 .env 設定 LLM_ROUTER_MODEL，或使用 --router-model 供應商:模型ID')
    client = create_client(spec, purpose='router')
    answer_spec = args.answer_model or settings.answer_model()
    answer_client = create_client(answer_spec, purpose='answer') if answer_spec else None
    with open_database(args.use_env_db) as (db, source):
        user = department_user(db, args.department)
        print(f'部門 {args.department.upper()}｜路由 {spec}｜回答 {answer_spec or "（未設定，只顯示段落）"}｜資料庫 {source}\n'
              '輸入問題；數字選擇選項；/new 新問題；/quit 離開')
        conv, options = Conversation(), []
        while True:
            try:
                text = input('\n> ').strip()
            except (EOFError, KeyboardInterrupt):
                break
            if not text:
                continue
            if text == '/quit':
                break
            if text == '/new':
                conv.reset()
                options = []
                print('（已清除，請輸入新問題）')
                continue
            if text.isdigit() and 1 <= int(text) <= len(options):
                text = options[int(text) - 1]
                print(f'（選擇：{text}）')
            turn = run_turn(db, user, text, conv, router_client=client, answer_client=answer_client)
            show(turn.route, client)
            if turn.route.error == 'invalid_input':
                continue
            if turn.route.action == 'remember':
                print('\n（命令列不儲存記憶；對話維持原狀）')
                continue
            if turn.route.action == 'clarify':
                options = turn.route.decision.clarify.options
                continue
            show_answer(turn, client)
            # No follow-up conversation yet (phase 4 decides): the next input starts a new question.
            options = []
            print('\n（下一個輸入視為新問題）')
    return 0


if __name__ == '__main__':
    sys.exit(main())
