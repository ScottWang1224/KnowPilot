"""Answer generation: the second LLM call, writing a cited answer from the chunks the router chose.

Only chunks already filtered by the user's access reach this step. Every point must cite
the chunk_ids it was given; citations to anything else are removed on the server, and a
point left without citations is dropped, so no unsupported statement reaches the user.
`missing` carries no citations, so it is dropped whenever the answer is complete.
"""
import hashlib
import json
import logging
from dataclasses import dataclass, field
from typing import Literal

from pydantic import BaseModel, model_validator

from core.config import ROOT
from llm import guard, settings
from llm.base import LLMResult
from llm.client import create_client

log = logging.getLogger('knowpilot.rag')
PROMPT_FILE = ROOT/'prompts'/'answer.md'
MAX_POINTS = 8
COMPACT_CHARS = 600
NOT_FOUND_TEXT = '目前可查閱的文件中找不到這個問題的答案。'
OUT_OF_SCOPE_TEXT = '目前可查閱的文件中沒有這方面的資料。'


class Point(BaseModel):
    text: str                     # one point, one or two sentences
    citations: list[str]          # chunk_ids such as "MFG-SOP-001#3" or "ANN:ecn-2026-011#1"
    condition: str | None = None  # with several possibilities: when this point applies (e.g. "CL-01", "正職")


class SafetyNote(BaseModel):
    text: str
    citations: list[str]


class AnswerOutput(BaseModel):
    status: Literal['answered', 'partial', 'not_found']
    safety_note: SafetyNote | None = None
    points: list[Point] = []
    missing: str | None = None    # partial / not_found: what the documents do not cover

    @model_validator(mode='after')
    def points_match_status(self):
        if self.status == 'not_found' and self.points:
            raise ValueError('status 為 not_found 時 points 必須為空')
        if self.status != 'not_found':
            if not self.points:
                raise ValueError(f'status 為 {self.status} 時 points 至少 1 個')
            if any(not p.citations for p in self.points):
                raise ValueError('每個要點的 citations 至少 1 個')
        return self


@dataclass
class AnswerResult:
    output: AnswerOutput | None          # answer after server-side citation checks
    status: str                          # answered | partial | not_found | skipped | error
    error: str | None = None             # budget_exceeded | busy | validation_failed | provider_error
    removed_citations: list[str] = field(default_factory=list)
    dropped_points: int = 0
    dropped_missing: bool = False        # an answered output carried `missing`, which was removed
    llm: LLMResult | None = None


def prompt_version():
    text = PROMPT_FILE.read_text(encoding='utf-8')
    return text, hashlib.sha256(text.encode()).hexdigest()[:12]


def build_user_message(question, route, preferences=None):
    """preferences: validated answer preferences ({'answer_style': '簡短'}); empty or None adds no block."""
    decision = route.decision
    intent = {'intent_summary': decision.intent_summary if decision else '', 'state': decision.state if decision else {}}
    multiple = bool(decision and decision.multiple_possibilities)
    sources = '\n\n'.join(f'[{hit.chunk_id}]\n{hit.text}' for hit in route.chunks)
    preferred = f'<preferences>{json.dumps(preferences, ensure_ascii=False)}</preferences>\n' if preferences else ''
    return (f'<intent>{json.dumps(intent, ensure_ascii=False)}</intent>\n'
            + preferred +
            f'<multiple_possibilities>{"true" if multiple else "false"}</multiple_possibilities>\n'
            f'<sources>\n{sources}\n</sources>\n'
            f'<question>{question}</question>')


def verify(output, allowed):
    """Keep only citations to chunks that were sent. Returns (output, removed citations, dropped points).

    `missing` is uncited free text: it may only say what the documents lack (partial / not_found),
    so on an answered output it is removed rather than shown without any check.
    """
    removed = []

    def keep(citations):
        removed.extend(c for c in citations if c not in allowed)
        return [c for c in dict.fromkeys(citations) if c in allowed]

    points = []
    for point in output.points:
        citations = keep(point.citations)
        if citations:
            points.append(point.model_copy(update={'citations': citations}))
    dropped = len(output.points) - len(points)
    safety = output.safety_note
    if safety:
        citations = keep(safety.citations)
        safety = safety.model_copy(update={'citations': citations}) if citations else None
    status = output.status
    if status != 'not_found' and not points:
        status = 'not_found'
    missing = None if status == 'answered' else output.missing
    return (output.model_copy(update={'status': status, 'points': points[:MAX_POINTS], 'safety_note': safety, 'missing': missing}),
            removed, dropped)


def answer(db, user, question, route, *, history=(), client=None, preferences=None):
    """Answer from route.chunks. db and user are kept for the phase 4 API; access was already applied by the router.
    preferences only change length and detail; citation, refusal and safety rules stay the same."""
    if route.error == 'invalid_input' or route.action in ('clarify', 'out_of_scope', 'remember'):
        return _logged(AnswerResult(None, 'skipped'), route)
    if route.error in ('budget_exceeded', 'busy'):
        # The answer call would be refused for the same reason; the caller lists route.chunks instead.
        return _logged(AnswerResult(None, 'error', error=route.error), route)
    if not route.chunks:
        return _logged(AnswerResult(AnswerOutput(status='not_found'), 'not_found'), route)
    if client is None:
        spec = settings.answer_model()
        if not spec:
            raise RuntimeError('未設定 LLM_ANSWER_MODEL（格式：供應商:模型 ID），請在 .env 設定或傳入 client')
        client = create_client(spec, purpose='answer')
    prompt_text, prompt_hash = prompt_version()
    messages = guard.trim_history(list(history)) + [{'role': 'user', 'content': build_user_message(question, route, preferences)}]
    result = client.complete_json(system=prompt_text, messages=messages, schema=AnswerOutput,
                                  max_tokens=settings.answer_max_tokens(), prompt_name=PROMPT_FILE.name,
                                  prompt_hash=prompt_hash)
    if result.error or result.data is None:
        return _logged(AnswerResult(None, 'error', error=result.error or 'validation_failed', llm=result), route)
    output, removed, dropped = verify(result.data, {hit.chunk_id for hit in route.chunks})
    if removed:
        log.warning('answer removed citations outside the sources: %s', ','.join(removed))
    dropped_missing = result.data.missing is not None and output.missing is None
    if dropped_missing:
        log.warning('answer removed `missing` from an answered output')
    return _logged(AnswerResult(output, output.status, removed_citations=removed, dropped_points=dropped,
                                dropped_missing=dropped_missing, llm=result), route)


def _logged(result, route):
    # Never log the question, chunks or answer text.
    log.info(json.dumps({'answer_status': result.status, 'error': result.error,
                         'removed_citations': result.removed_citations, 'dropped_points': result.dropped_points,
                         'dropped_missing': result.dropped_missing,
                         'chunks': len(route.chunks), 'points': len(result.output.points) if result.output else 0},
                        ensure_ascii=False))
    return result


def condition_repeated(point):
    """The point's text starts with its own condition (shown twice once the condition becomes a tag)."""
    return bool(point.condition) and point.text.lstrip('（( ').startswith(point.condition.strip())


def compact_answer(output, limit=COMPACT_CHARS):
    """The answer kept in conversation history: safety note and points (with conditions), no citations."""
    if output.status == 'not_found':
        return NOT_FOUND_TEXT
    lines = [f'【安全提示】{output.safety_note.text}'] if output.safety_note else []
    lines += [f'- {f"（{p.condition}）" if p.condition else ""}{p.text}' for p in output.points]
    return '\n'.join(lines)[:limit]


def format_answer(output):
    """Plain-text answer for the CLI and the evaluation (key-fact judge, labelling sheet)."""
    if output is None:
        return ''
    lines = []
    if output.safety_note:
        lines.append(f'【安全提示】{output.safety_note.text}〔{"、".join(output.safety_note.citations)}〕')
    if output.status == 'not_found':
        lines.append(NOT_FOUND_TEXT)
    for i, point in enumerate(output.points, 1):
        condition = f'（{point.condition}）' if point.condition else ''
        lines.append(f'{i}. {condition}{point.text}〔{"、".join(point.citations)}〕')
    if output.missing:
        lines.append(f'找不到的部分：{output.missing}')
    return '\n'.join(lines)
