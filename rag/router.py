"""Router: the first LLM call. Decides answer / clarify / out_of_scope and picks the chunks to answer from.

Everything put into the prompt comes from what the user may see (visible_catalog,
search, visible chunks for dimensions, the glossary through document_scope). The
model's choices are checked again on the server: unseen doc codes are dropped and
any LLM failure falls back to plain BM25, so the user always gets documents.
"""
import hashlib
import json
import logging
import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, model_validator

from core.config import ROOT
from llm import guard, settings
from llm.base import LLMResult
from llm.client import create_client
from rag import glossary, index, memory as memories
from rag.index import ChunkHit

log = logging.getLogger('knowpilot.rag')
PROMPT_FILE = ROOT/'prompts'/'router.md'
DIMENSIONS_FILE = Path(__file__).with_name('clarify_dimensions.yaml')
MAX_CLARIFY_TURNS = 3
PROMPT_CHUNKS, PROMPT_CHUNK_CHARS = 8, 200
FALLBACK_CHUNKS = 5
MAX_OPTIONS, MAX_STATE_KEYS, MAX_STATE_CHARS = 5, 12, 80
MAX_QUERIES, MAX_QUERY_CHARS = 4, 50
MAX_SELECTED_DOCS, MAX_CHUNKS_PER_DOC, MAX_SELECTED_CHUNKS = 5, 3, 8
BM25_CHUNKS_PER_DOC = 2
EXTRA_PER_QUERY, MAX_EXTRA_CHUNKS = 3, 4


class Selected(BaseModel):
    doc_code: str
    sections: list[str] = []


class Clarify(BaseModel):
    dimension: str
    question: str
    options: list[str]
    safety_note: str | None = None


class MemoryItem(BaseModel):
    key: str
    value: str


class RouterOutput(BaseModel):
    action: Literal['answer', 'clarify', 'out_of_scope', 'remember']
    intent_summary: str = ''
    state: dict[str, Any] = {}
    rewritten_queries: list[str] = []
    selected: list[Selected] = []
    clarify: Clarify | None = None
    multiple_possibilities: bool = False
    used_memory: list[str] = []                # keys of <memory> the model relied on
    memory_proposal: list[MemoryItem] = []     # only when the user explicitly asked to remember

    @model_validator(mode='after')
    def clarify_needs_options(self):
        if self.action == 'clarify' and (self.clarify is None or len(self.clarify.options) < 2):
            raise ValueError('action 為 clarify 時，clarify 必填且 options 至少 2 個')
        return self


@dataclass
class RouteResult:
    action: str                          # answer | clarify | out_of_scope | remember (check error first)
    decision: RouterOutput | None        # model output after server-side corrections
    chunks: list[ChunkHit]               # chunks for the answer model; [] for clarify / out_of_scope / remember
    fallback: str | None = None          # None | 'llm_error' | 'empty_selection'
    error: str | None = None             # invalid_input | budget_exceeded | busy | validation_failed | provider_error
    message: str | None = None           # Chinese message for invalid_input
    dropped_docs: list[str] = field(default_factory=list)
    dropped_memory: int = 0              # memory_proposal items outside the user's memory options
    llm: LLMResult | None = None


def nfkc(text):
    return unicodedata.normalize('NFKC', text)


# ---- clarify dimensions -------------------------------------------------------------------

def load_dimensions(path=DIMENSIONS_FILE):
    return yaml.safe_load(path.read_text(encoding='utf-8'))['dimensions']


def filter_dimensions(dimensions, visible_text):
    """Keep values whose match strings appear in visible_text; drop dimensions left with < 2 values."""
    text = nfkc(visible_text)
    kept = {}
    for key, dim in dimensions.items():
        values = [v for v in dim['values'] if any(nfkc(m) in text for m in v['match'])]
        if len(values) >= 2:
            kept[key] = {**dim, 'values': values}
    return {key: dim for key, dim in kept.items() if dim.get('depends_on') in (None, *kept)}


def visible_dimensions(db, user, dimensions=None):
    allowed = index.visible_codes(db, user)
    text = '\n'.join(c.indexed_text for c in index.get_index(db).chunks if c.doc_code in allowed)
    return filter_dimensions(load_dimensions() if dimensions is None else dimensions, text)


def memory_options(db, user):
    """{key: [values]} the user may remember: their visible clarify dimensions plus the answer style."""
    return memories.options_from(visible_dimensions(db, user))


def format_dimensions(dimensions):
    """key（label）：值1（白話）、值2…, one dimension per line."""
    return '\n'.join(f'{key}（{dim["label"]}）：'
                     + '、'.join(f'{v["value"]}（{v["label"]}）' if v.get('label') else str(v['value']) for v in dim['values'])
                     for key, dim in dimensions.items())


# ---- prompt -------------------------------------------------------------------------------

def format_catalog(catalog):
    lines = []
    for d in catalog['documents']:
        applies = '；'.join(f'{k}={",".join(map(str, v)) if isinstance(v, list) else v}' for k, v in (d['applies_to'] or {}).items())
        lines.append(f'- {d["doc_code"]}｜{d["title"]}｜{d["category"]}｜{d["summary"]}' + (f'｜適用：{applies}' if applies else ''))
    lines += [f'- {a["doc_code"]}｜{a["title"]}｜公告' for a in catalog['announcements']]
    return '\n'.join(lines)


def prompt_version():
    """(router.md text, sha256[:12]); the hash identifies the prompt file, not the per-user data."""
    text = PROMPT_FILE.read_text(encoding='utf-8')
    return text, hashlib.sha256(text.encode()).hexdigest()[:12]


def build_system(db, user, prompt_text, *, use_glossary=True):
    """Stable → variable: prompt file, glossary, dimensions, catalog. The client appends the JSON schema."""
    parts = [prompt_text.strip()]
    terms = glossary.format_for_prompt(glossary.load(db, user)) if use_glossary else ''
    if terms:
        parts.append('## 術語對照表（正式術語：常見口語說法）\n' + terms)
    parts.append('## 澄清維度\n' + (format_dimensions(visible_dimensions(db, user)) or '（無）'))
    parts.append('## 文件目錄（文件編號｜標題｜分類｜摘要｜適用範圍）\n' + format_catalog(index.visible_catalog(db, user)))
    return '\n\n'.join(parts)


def chunk_preview(hit):
    body = hit.text.split('\n', 1)[1] if '\n' in hit.text else ''
    return f'【{hit.doc_code} {hit.doc_title}｜{hit.section_path}】\n{body[:PROMPT_CHUNK_CHARS]}'


def build_user_message(question, hits, state, clarify_turns, memory=None, options=None):
    """Memory goes in the user turn, not the system prompt, so each department's system prompt stays the same."""
    previews = '\n\n'.join(chunk_preview(h) for h in hits) or '（沒有相關段落）'
    remembered = '' if memory is None else (f'<memory>{json.dumps(memory, ensure_ascii=False)}</memory>\n'
                                            f'<memory_options>\n{memories.format_options(options)}\n</memory_options>\n')
    return (f'<clarify_turns>{clarify_turns}</clarify_turns>\n'
            f'<state>{json.dumps(state or {}, ensure_ascii=False)}</state>\n'
            + remembered +
            f'<search_results>\n{previews}\n</search_results>\n'
            f'<question>{question}</question>')


# ---- server-side corrections and chunk selection ------------------------------------------

def correct(decision, visible, clarify_turns, memory=None, options=None):
    """Apply the server-side limits. Returns (corrected decision, dropped doc codes, dropped memory proposals).

    Memory proposals outside the options are dropped like invisible doc codes; used_memory keeps
    only keys that were in <memory>.
    """
    selected, dropped = [], []
    for item in decision.selected:
        if item.doc_code not in visible:
            dropped.append(item.doc_code)
        elif all(s.doc_code != item.doc_code for s in selected):
            selected.append(item)
    state = {k: (v if isinstance(v, str) else json.dumps(v, ensure_ascii=False))[:MAX_STATE_CHARS]
             for k, v in list(decision.state.items())[:MAX_STATE_KEYS] if v is not None}
    update = {'selected': selected[:MAX_SELECTED_DOCS], 'state': state,
              'rewritten_queries': [q[:MAX_QUERY_CHARS] for q in decision.rewritten_queries if q.strip()][:MAX_QUERIES]}
    proposal = [item for item in decision.memory_proposal if memories.is_valid(options or {}, item.key, item.value)]
    update['memory_proposal'] = list({item.key: item for item in proposal}.values())
    update['used_memory'] = [k for k in dict.fromkeys(decision.used_memory) if k in (memory or {})]
    if decision.clarify:
        update['clarify'] = decision.clarify.model_copy(update={'options': decision.clarify.options[:MAX_OPTIONS]})
    if decision.action == 'clarify' and clarify_turns >= MAX_CLARIFY_TURNS:
        update.update(action='answer', clarify=None, multiple_possibilities=True)
    return decision.model_copy(update=update), dropped, len(decision.memory_proposal) - len(proposal)


def normalize_section(text):
    return re.sub(r'\s+', '', nfkc(text))


def match_sections(doc_chunks, sections):
    """Chunks whose section_path equals, starts with, or contains a requested section (first rule that hits)."""
    found = []
    for wanted in map(normalize_section, sections):
        if not wanted:
            continue
        for rule in (str.__eq__, str.startswith, str.__contains__):
            hits = [c for c in doc_chunks if rule(normalize_section(c.section_path), wanted)]
            if hits:
                found += [c for c in hits if c not in found]
                break
    return found


def as_hit(chunk):
    return ChunkHit(chunk.chunk_id, chunk.doc_code, chunk.doc_title, chunk.section_path, chunk.indexed_text, 0.0)


def select_chunks(db, user, question, decision):
    chunks = index.get_index(db).chunks
    query = ' '.join([question, *decision.rewritten_queries])
    picked = []
    for item in decision.selected:
        doc_chunks = [c for c in chunks if c.doc_code == item.doc_code]
        hits = [as_hit(c) for c in match_sections(doc_chunks, item.sections)]
        hits = hits or index.search(db, user, query, k=BM25_CHUNKS_PER_DOC, restrict_docs={item.doc_code})
        # No query word occurs in the document (e.g. the reply was just 「不知道」): keep its opening chunks.
        hits = hits or [as_hit(c) for c in doc_chunks[:BM25_CHUNKS_PER_DOC]]
        picked += hits[:MAX_CHUNKS_PER_DOC]
    picked = list({h.chunk_id: h for h in picked}.values())[:MAX_SELECTED_CHUNKS]
    seen, extra = {h.chunk_id for h in picked}, 0
    for q in decision.rewritten_queries:
        for hit in index.search(db, user, q, k=EXTRA_PER_QUERY):
            if extra < MAX_EXTRA_CHUNKS and hit.chunk_id not in seen:
                picked.append(hit)
                seen.add(hit.chunk_id)
                extra += 1
    return picked


# ---- entry point --------------------------------------------------------------------------

def route(db, user, question, *, history=(), state=None, clarify_turns=0, client=None, use_glossary=True,
          memory=None):
    """memory: the user's validated work context ({key: value}, possibly empty) from the chat API.
    None (evaluation, CLI) leaves the memory blocks out of the prompt."""
    if client is None:
        spec = settings.router_model()
        if not spec:
            raise RuntimeError('未設定 LLM_ROUTER_MODEL（格式：供應商:模型 ID），請在 .env 設定或傳入 client')
        client = create_client(spec, purpose='router')
    problem = guard.check_question(question)
    if problem:
        return _logged(RouteResult('answer', None, [], error='invalid_input', message=problem), clarify_turns)

    prompt_text, prompt_hash = prompt_version()
    system = build_system(db, user, prompt_text, use_glossary=use_glossary)
    hits = index.search(db, user, question, k=PROMPT_CHUNKS)
    options = None if memory is None else memory_options(db, user)
    messages = guard.trim_history(list(history)) + [
        {'role': 'user', 'content': build_user_message(question, hits, state, clarify_turns, memory, options)}]
    result = client.complete_json(system=system, messages=messages, schema=RouterOutput,
                                  max_tokens=settings.router_max_tokens(), prompt_name=PROMPT_FILE.name,
                                  prompt_hash=prompt_hash)
    if result.error or result.data is None:
        # BM25 costs nothing, so even with the budget used up the user still gets related documents.
        return _logged(RouteResult('answer', None, hits[:FALLBACK_CHUNKS], fallback='llm_error',
                                   error=result.error or 'validation_failed', llm=result), clarify_turns)

    if options is None and result.data.memory_proposal:
        options = memory_options(db, user)   # proposals are checked even without memory blocks (evaluation, CLI)
    decision, dropped, dropped_memory = correct(result.data, index.visible_codes(db, user), clarify_turns,
                                                memory, options)
    if dropped:
        log.warning('router dropped invisible doc codes: %s', ','.join(dropped))
    if dropped_memory:
        log.warning('router dropped %d memory proposals outside the options', dropped_memory)
    route_result = RouteResult(decision.action, decision, [], dropped_docs=dropped, dropped_memory=dropped_memory,
                               llm=result)
    if decision.action == 'answer':
        route_result.chunks = select_chunks(db, user, question, decision)
        if not route_result.chunks:
            route_result.chunks, route_result.fallback = hits[:FALLBACK_CHUNKS], 'empty_selection'
    return _logged(route_result, clarify_turns)


def _logged(result, clarify_turns):
    # Never log the question or chunk text.
    log.info(json.dumps({'action': result.action, 'fallback': result.fallback, 'error': result.error,
                         'dropped_docs': result.dropped_docs, 'dropped_memory': result.dropped_memory, 'chunks': len(result.chunks),
                         'clarify_turns': clarify_turns}, ensure_ascii=False))
    return result
