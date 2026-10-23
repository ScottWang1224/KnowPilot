"""LLM settings from environment variables, read at call time so tests and scripts can override them."""
import os


def _float(name, default):
    return float(os.getenv(name) or default)


def _int(name, default):
    return int(os.getenv(name) or default)


def timeout_seconds():
    return _float('LLM_TIMEOUT_SECONDS', 30)


def daily_budget_usd():
    return _float('LLM_DAILY_BUDGET_USD', 2)


def fallback_price_per_mtok():
    return _float('LLM_FALLBACK_PRICE_PER_MTOK', 10)


def max_concurrency():
    return _int('LLM_MAX_CONCURRENCY', 5)


def router_max_tokens():
    return _int('LLM_ROUTER_MAX_TOKENS', 800)


def answer_max_tokens():
    return _int('LLM_ANSWER_MAX_TOKENS', 1200)


def chat_max_input_chars():
    return _int('CHAT_MAX_INPUT_CHARS', 500)


def chat_user_daily_budget_usd():
    return _float('CHAT_USER_DAILY_BUDGET_USD', 0.05)


def chat_default_cost_per_question_usd():
    return _float('CHAT_DEFAULT_COST_PER_QUESTION_USD', 0.0025)


def router_model():
    return os.getenv('LLM_ROUTER_MODEL', '')


def answer_model():
    return os.getenv('LLM_ANSWER_MODEL', '')


def judge_model():
    return os.getenv('LLM_JUDGE_MODEL', '')


def judge_max_tokens():
    return _int('LLM_JUDGE_MAX_TOKENS', 2000)
