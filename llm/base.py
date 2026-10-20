"""Shared types for every provider adapter."""
from dataclasses import dataclass
from typing import Protocol

from pydantic import BaseModel

# LLMResult.error codes callers can branch on.
BUDGET_EXCEEDED = 'budget_exceeded'      # daily budget used up: show "今日體驗額度已用完"
BUSY = 'busy'                            # concurrency limit reached: show "系統忙碌"
VALIDATION_FAILED = 'validation_failed'  # output was not valid JSON for the schema after one retry
PROVIDER_ERROR = 'provider_error'        # network, timeout, HTTP error or refusal after one retry


@dataclass
class LLMResult:
    data: BaseModel | None          # validated structured output
    raw_text: str
    provider: str
    model: str
    input_tokens: int
    output_tokens: int
    latency_ms: int
    cost_usd: float | None          # from llm/models.yaml prices; None when the model has no price
    error: str | None
    budget_cost_usd: float = 0.0    # what the daily budget was charged (fallback price when unpriced)
    attempts: int = 0
    validation_failed: bool = False
    prompt_hash: str = ''
    error_detail: str | None = None  # short, key-free description for logs


@dataclass
class ProviderReply:
    text: str
    input_tokens: int
    output_tokens: int


class ProviderError(Exception):
    """A provider call failed (network, timeout, HTTP status, refusal). Message must never contain a key.

    Billed failures (a refusal still consumes tokens) carry their usage so the budget is charged.
    """

    def __init__(self, message, *, input_tokens=0, output_tokens=0):
        super().__init__(message)
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens


class Provider(Protocol):
    name: str

    def complete(self, *, model: str, system: str, messages: list[dict], max_tokens: int,
                 timeout: float, config: dict) -> ProviderReply: ...


class LLMClient(Protocol):
    def complete_json(self, *, system: str, messages: list[dict], schema: type[BaseModel],
                      max_tokens: int) -> LLMResult: ...
