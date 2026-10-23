"""Run explicitly: python -m scripts.chat_cli --department MFG. Multi-turn router prototype.

Shows what the router decides each turn (clarifying question, selected chunks, tokens,
latency, cost). No final answer is generated in phase 2. Uses a fresh temporary SQLite
built from data/ unless --use-env-db is given; LLM settings and keys come from .env.
Commands: /new starts a new question, /quit exits. Type an option number to pick it.
"""
import argparse
import json
import sys

from dotenv import load_dotenv
from core.config import ROOT
from llm import settings
from llm.client import create_client
from rag import router
from scripts.local_db import open_database


def department_user(db, code):
    """In-memory user of a department; access still goes through document_scope() in SQL."""
    row = db.query('SELECT id FROM departments WHERE code=%s', (code.upper(),), one=True)
    if not row:
        raise SystemExit(f'找不到部門代碼：{code}')
    return {'id': 0, 'department_id': row['id']}


def clarify_text(clarify):
    """The assistant turn kept in history: the question and its numbered options."""
    return clarify.question + ''.join(f'\n{i}. {option}' for i, option in enumerate(clarify.options, 1))


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
    if result.dropped_docs:
        print(f'[丟棄的文件] {", ".join(result.dropped_docs)}')
    if result.chunks:
        print('[要送給回答模型的段落]')
        for hit in result.chunks:
            print(f'  {hit.chunk_id:<16} {hit.section_path}')
    if result.llm:
        llm = result.llm
        cost = '無價格資料' if llm.cost_usd is None else f'${llm.cost_usd:.6f}'
        print(f'[LLM] {llm.provider}:{llm.model}  tokens in/out {llm.input_tokens}/{llm.output_tokens}  '
              f'嘗試 {llm.attempts} 次{"（曾驗證失敗）" if llm.validation_failed else ""}  延遲 {llm.latency_ms} ms')
        print(f'[成本] 實際 {cost}  預算計入 ${llm.budget_cost_usd:.6f}  今日累計 ${client.budget.spent:.6f}')


def main():
    sys.stdout.reconfigure(encoding='utf-8')
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--department', required=True, help='department code, e.g. MFG, PE, QA, RD, ADM')
    parser.add_argument('--router-model', help='provider:model-id, overrides LLM_ROUTER_MODEL')
    parser.add_argument('--use-env-db', action='store_true', help='use the database from .env instead of a fresh temporary one')
    args = parser.parse_args()
    load_dotenv(ROOT/'.env')
    spec = args.router_model or settings.router_model()
    if not spec:
        raise SystemExit('未設定路由模型：請在 .env 設定 LLM_ROUTER_MODEL，或使用 --router-model 供應商:模型ID')
    client = create_client(spec, purpose='router')
    with open_database(args.use_env_db) as (db, source):
        user = department_user(db, args.department)
        print(f'部門 {args.department.upper()}｜模型 {spec}｜資料庫 {source}\n輸入問題；數字選擇選項；/new 新問題；/quit 離開')
        history, state, turns, options = [], None, 0, []
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
                history, state, turns, options = [], None, 0, []
                print('（已清除，請輸入新問題）')
                continue
            if text.isdigit() and 1 <= int(text) <= len(options):
                text = options[int(text) - 1]
                print(f'（選擇：{text}）')
            result = router.route(db, user, text, history=history, state=state, clarify_turns=turns, client=client)
            show(result, client)
            if result.error == 'invalid_input':
                continue
            if result.action == 'clarify':
                history += [{'role': 'user', 'content': text},
                            {'role': 'assistant', 'content': clarify_text(result.decision.clarify)}]
                state, turns, options = result.decision.state, turns + 1, result.decision.clarify.options
            else:
                # Phase 2 has no follow-up conversation: the next input starts a new question.
                history, state, turns, options = [], None, 0, []
                print('\n（下一個輸入視為新問題）')
    return 0


if __name__ == '__main__':
    sys.exit(main())
