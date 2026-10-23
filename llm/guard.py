"""Cost guards every LLM call passes through: daily budget, concurrency and input limits.

DailyBudget keeps the daily total in memory (it resets on restart and the reset is logged)
and serves chat_cli and the evaluation scripts. The chat API uses DbBudget, computed from
the llm_calls records, so a restart never resets it.
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


def taipei_day_bounds(now=None):
    """[start, end) of the Taipei day containing `now`, as naive UTC datetimes for SQL on either engine."""
    now = now or datetime.now(timezone.utc)
    start = datetime.combine(now.astimezone(TAIPEI).date(), datetime.min.time(), TAIPEI)
    start = start.astimezone(timezone.utc).replace(tzinfo=None)
    return start, start + timedelta(days=1)


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


class DbBudget:
    """Same interface as DailyBudget, but the day's total comes from llm_calls.budget_cost_usd.

    Create one per chat request. charge() only adds to `pending` (charged but not yet written);
    the call recorder writes the llm_calls row and then calls clear_pending().
    """

    def __init__(self, db, limit_usd=None, now=None):
        self.db = db
        self._limit = limit_usd
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._lock = threading.Lock()
        self.pending = 0.0

    @property
    def limit(self):
        return settings.daily_budget_usd() if self._limit is None else self._limit

    def recorded(self):
        start, end = taipei_day_bounds(self._now())
        row = self.db.query('SELECT COALESCE(SUM(budget_cost_usd),0) AS total FROM llm_calls '
                            'WHERE created_at>=%s AND created_at<%s', (start, end), one=True)
        return float(row['total'])

    def allows_call(self):
        spent, limit = self.spent, self.limit
        if spent >= limit:
            log.warning('llm daily budget reached (db): spent=%.4f limit=%.4f', spent, limit)
        return spent < limit

    def charge(self, amount):
        with self._lock:
            self.pending += amount

    def clear_pending(self):
        with self._lock:
            self.pending = 0.0

    @property
    def spent(self):
        return self.recorded() + self.pending


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
