"""Cost guards every LLM call passes through: daily budget, concurrency and input limits.

Phase 2 keeps the daily total in memory (it resets on restart and the reset is logged);
phase 4 will compute it from the stored call records instead.
"""
import logging
import threading
from datetime import datetime, timedelta, timezone

from llm import settings

log = logging.getLogger('knowpilot.llm')
TAIPEI = timezone(timedelta(hours=8))
HISTORY_TURNS = 6


def taipei_today():
    return datetime.now(TAIPEI).date()


class DailyBudget:
    """Spending breaker per Taipei calendar day. Once spent >= limit, new calls are refused."""

    def __init__(self, limit_usd=None, today=taipei_today):
        self._limit = limit_usd
        self._today = today
        self._lock = threading.Lock()
        self._day = today()
        self._spent = 0.0
        log.info('llm budget started: day=%s spent=0 (in-memory, resets on restart)', self._day)

    @property
    def limit(self):
        return settings.daily_budget_usd() if self._limit is None else self._limit

    def _roll(self):
        day = self._today()
        if day != self._day:
            log.info('llm budget reset: %s spent=%.4f -> %s', self._day, self._spent, day)
            self._day, self._spent = day, 0.0

    def allows_call(self):
        with self._lock:
            self._roll()
            return self._spent < self.limit

    def charge(self, amount):
        with self._lock:
            self._roll()
            self._spent += amount
            if self._spent >= self.limit:
                log.warning('llm daily budget reached: spent=%.4f limit=%.4f', self._spent, self.limit)

    @property
    def spent(self):
        with self._lock:
            self._roll()
            return self._spent


class ConcurrencyGate:
    """Caps simultaneous LLM calls. Refuses immediately instead of queueing."""

    def __init__(self, limit=None):
        self._semaphore = threading.BoundedSemaphore(limit or settings.max_concurrency())

    def try_enter(self):
        return self._semaphore.acquire(blocking=False)

    def leave(self):
        self._semaphore.release()


_default_budget = None
_default_gate = None
_defaults_lock = threading.Lock()


def default_budget():
    global _default_budget
    with _defaults_lock:
        if _default_budget is None:
            _default_budget = DailyBudget()
        return _default_budget


def default_gate():
    global _default_gate
    with _defaults_lock:
        if _default_gate is None:
            _default_gate = ConcurrencyGate()
        return _default_gate


def check_question(text):
    """Return an error message when a user question breaks the input limits, otherwise None.

    Callers check this before any LLM call; phase 4's chat API turns it into HTTP 400.
    """
    if not text or not text.strip():
        return '問題不可為空白'
    limit = settings.chat_max_input_chars()
    if len(text) > limit:
        return f'問題超過 {limit} 字上限'
    return None


def trim_history(messages, turns=HISTORY_TURNS):
    """Keep only the most recent turns (one user + one assistant message per turn)."""
    return messages[-turns * 2:]
