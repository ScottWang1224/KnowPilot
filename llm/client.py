"""Structured JSON calls on top of any provider adapter.

The JSON schema is written into the system prompt and the reply is validated with
Pydantic, so no provider-specific structured-output feature is required. A reply that
fails validation is retried once with the error attached; a provider failure
(network, timeout, HTTP error) is also retried once. Every attempt goes through the
daily budget and the concurrency gate.
"""
import hashlib
import json
import logging
import re
import time

from pydantic import ValidationError

from llm import guard, settings
from llm.base import (BUDGET_EXCEEDED, BUSY, PROVIDER_ERROR, VALIDATION_FAILED, LLMResult,
                      ProviderError)
from llm.pricing import Pricing

log = logging.getLogger('knowpilot.llm')
MAX_ATTEMPTS = 2

SCHEMA_INSTRUCTION = ('\n\n## 輸出格式\n只回傳一個 JSON 物件，不要加任何說明文字或 Markdown 標記。'
                      'JSON 必須符合以下 JSON Schema：\n{schema}')
RETRY_INSTRUCTION = '上一次的輸出無法通過驗證：{error}\n請重新輸出，只回傳一個符合 JSON Schema 的 JSON 物件。'


def extract_json(text):
    """The JSON object inside a reply, tolerating code fences or stray text around it."""
    text = re.sub(r'^\s*```(?:json)?\s*|\s*```\s*$', '', text.strip())
    start, end = text.find('{'), text.rfind('}')
    if start < 0 or end < start:
        raise ValueError('回覆中沒有 JSON 物件')
    return json.loads(text[start:end + 1])


class LLM:
    """An LLMClient for one 'provider:model-id' spec and one purpose (router / answer)."""

    def __init__(self, provider, spec, *, purpose, pricing=None, budget=None, gate=None, recorder=None):
        """recorder(result, purpose), when given, runs after every call, failed ones included."""
        self.provider = provider
        self.spec = spec
        self.model = spec.split(':', 1)[1]
        self.purpose = purpose
        self.pricing = pricing or Pricing()
        self.budget = budget or guard.default_budget()
        self.gate = gate or guard.default_gate()
        self.recorder = recorder

    def complete_json(self, *, system, messages, schema, max_tokens, prompt_name=None, prompt_hash=None):
        """prompt_name/prompt_hash identify the prompt file version when system also carries per-user data."""
        full_system = system + SCHEMA_INSTRUCTION.format(
            schema=json.dumps(schema.model_json_schema(), ensure_ascii=False))
        result = LLMResult(data=None, raw_text='', provider=self.provider.name, model=self.model,
                           input_tokens=0, output_tokens=0, latency_ms=0, cost_usd=0.0, error=None,
                           prompt_hash=prompt_hash or hashlib.sha256(system.encode()).hexdigest()[:12],
                           prompt_name=prompt_name or '')
        if not self.gate.try_enter():
            result.error = BUSY
            return self._log(result)
        try:
            self._run(result, full_system, list(messages), schema, max_tokens)
        finally:
            self.gate.leave()
        return self._log(result)

    def _run(self, result, system, messages, schema, max_tokens):
        cfg = self.pricing.config(self.spec, self.purpose)
        unpriced = False
        for attempt in range(1, MAX_ATTEMPTS + 1):
            if not self.budget.allows_call():
                result.error = BUDGET_EXCEEDED
                break
            result.attempts = attempt
            started = time.monotonic()
            try:
                reply = self.provider.complete(model=self.model, system=system, messages=messages,
                                               max_tokens=max_tokens, timeout=settings.timeout_seconds(),
                                               config=cfg)
            except (ProviderError, TimeoutError, OSError) as exc:
                result.latency_ms += int((time.monotonic() - started) * 1000)
                result.error, result.error_detail = PROVIDER_ERROR, str(exc)[:200]
                unpriced = self._account(result, getattr(exc, 'input_tokens', 0), getattr(exc, 'output_tokens', 0), unpriced)
                continue
            result.latency_ms += int((time.monotonic() - started) * 1000)
            result.raw_text = reply.text
            unpriced = self._account(result, reply.input_tokens, reply.output_tokens, unpriced)
            try:
                result.data = schema.model_validate(extract_json(reply.text))
                result.error = result.error_detail = None
                return
            except (ValueError, ValidationError) as exc:
                result.validation_failed = True
                result.error, result.error_detail = VALIDATION_FAILED, str(exc)[:200]
                messages = messages + [{'role': 'assistant', 'content': reply.text},
                                       {'role': 'user', 'content': RETRY_INSTRUCTION.format(error=str(exc)[:500])}]

    def _account(self, result, input_tokens, output_tokens, unpriced):
        """Add one attempt's tokens and cost; charge the daily budget. Returns whether any attempt was unpriced."""
        if not input_tokens and not output_tokens:
            return unpriced
        result.attempt_output_tokens.append(output_tokens)
        result.input_tokens += input_tokens
        result.output_tokens += output_tokens
        actual = self.pricing.actual_cost(self.spec, input_tokens, output_tokens)
        unpriced = unpriced or actual is None
        result.cost_usd = None if unpriced else result.cost_usd + actual
        charge = self.pricing.budget_cost(self.spec, input_tokens, output_tokens)
        result.budget_cost_usd += charge
        self.budget.charge(charge)
        return unpriced

    def _log(self, result):
        # Never log prompts, replies or keys: only what is needed to compare models and spot failures.
        log.info(json.dumps({'purpose': self.purpose, 'provider': result.provider, 'model': result.model,
                             'prompt_name': result.prompt_name, 'prompt_hash': result.prompt_hash,
                             'input_tokens': result.input_tokens,
                             'output_tokens': result.output_tokens, 'latency_ms': result.latency_ms,
                             'cost_usd': result.cost_usd, 'budget_cost_usd': round(result.budget_cost_usd, 6),
                             'attempts': result.attempts, 'validation_failed': result.validation_failed,
                             'error': result.error, 'error_detail': result.error_detail}, ensure_ascii=False))
        if self.recorder is not None:
            try:
                self.recorder(result, self.purpose)
            except Exception:
                # A lost record must never fail the user's request.
                log.exception('llm call recorder failed')
        return result


def provider_for(name):
    if name == 'fake':
        from llm.providers.fake import FakeProvider
        return FakeProvider()
    if name == 'openai_compatible':
        from llm.providers.openai_compatible import OpenAICompatibleProvider
        return OpenAICompatibleProvider()
    if name == 'anthropic':
        from llm.providers.anthropic import AnthropicProvider
        return AnthropicProvider()
    raise ValueError(f'未知的 LLM 供應商：{name}（可用：openai_compatible、anthropic、fake）')


def create_client(spec, *, purpose, **kwargs):
    """Build an LLMClient from a 'provider:model-id' spec such as LLM_ROUTER_MODEL."""
    if ':' not in spec:
        raise ValueError(f'模型設定格式應為「供應商:模型 ID」，收到：{spec!r}')
    return LLM(provider_for(spec.split(':', 1)[0]), spec, purpose=purpose, **kwargs)
