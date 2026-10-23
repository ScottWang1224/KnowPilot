"""LLM layer tests. Only the fake provider and stub clients are used: no network, no API key."""
from datetime import date
from types import SimpleNamespace

import pytest
from pydantic import BaseModel


class Answer(BaseModel):
    action: str
    docs: list[str]


VALID = '{"action": "answer", "docs": ["MFG-SOP-009"]}'
PRICED = {'fake:priced': {'input_per_mtok': 2, 'output_per_mtok': 10}}


def make(script, spec='fake:priced', *, models=PRICED, budget=None, gate=None):
    from llm.client import LLM
    from llm.guard import ConcurrencyGate, DailyBudget
    from llm.pricing import Pricing
    from llm.providers.fake import FakeProvider
    provider = FakeProvider(script)
    client = LLM(provider, spec, purpose='router', pricing=Pricing(models),
                 budget=budget or DailyBudget(limit_usd=100), gate=gate or ConcurrencyGate(limit=5))
    return client, provider


def call(client):
    return client.complete_json(system='路由', messages=[{'role': 'user', 'content': '條紋怎麼辦'}],
                                schema=Answer, max_tokens=800)


def test_valid_json_is_parsed_and_priced():
    client, provider = make(['說明文字 ```json\n' + VALID + '\n```'])
    result = call(client)
    assert result.error is None and result.data == Answer(action='answer', docs=['MFG-SOP-009'])
    assert (result.input_tokens, result.output_tokens, result.attempts) == (100, 50, 1)
    assert result.cost_usd == pytest.approx((100 * 2 + 50 * 10) / 1_000_000)
    assert result.budget_cost_usd == pytest.approx(result.cost_usd)
    assert '"action"' in provider.calls[0]['system'] and result.prompt_hash


def test_invalid_json_retries_once_with_the_error_then_gives_up():
    client, provider = make(['不是 JSON', '{"action": "answer"}'])
    result = call(client)
    assert result.data is None and result.error == 'validation_failed' and result.validation_failed
    assert result.attempts == 2 and len(provider.calls) == 2
    retry_messages = provider.calls[1]['messages']
    assert retry_messages[-2] == {'role': 'assistant', 'content': '不是 JSON'}
    assert '無法通過驗證' in retry_messages[-1]['content']
    assert result.input_tokens == 200  # both attempts are counted


def test_invalid_then_valid_json_succeeds():
    client, _ = make(['{"action": 1}', VALID])
    result = call(client)
    assert result.error is None and result.data.action == 'answer' and result.validation_failed


def test_unpriced_model_has_no_cost_but_still_charges_the_budget(monkeypatch):
    from llm.guard import DailyBudget
    monkeypatch.setenv('LLM_FALLBACK_PRICE_PER_MTOK', '10')
    budget = DailyBudget(limit_usd=100)
    client, _ = make([VALID], spec='fake:unpriced', budget=budget)
    result = call(client)
    assert result.error is None and result.cost_usd is None
    assert result.budget_cost_usd == pytest.approx(150 * 10 / 1_000_000)
    assert budget.spent == pytest.approx(result.budget_cost_usd)


def test_budget_exceeded_refuses_without_calling_the_provider():
    from llm.guard import DailyBudget
    budget = DailyBudget(limit_usd=0.0001)
    budget.charge(0.0001)
    client, provider = make([VALID], budget=budget)
    result = call(client)
    assert result.error == 'budget_exceeded' and result.data is None and provider.calls == []


def test_budget_stops_the_retry_once_the_first_attempt_reaches_the_limit():
    from llm.guard import DailyBudget
    budget = DailyBudget(limit_usd=0.0005)  # one priced attempt costs 0.0007
    client, provider = make(['不是 JSON', VALID], budget=budget)
    result = call(client)
    assert result.error == 'budget_exceeded' and len(provider.calls) == 1


def test_budget_resets_on_a_new_taipei_day():
    from llm.guard import DailyBudget
    days = iter([date(2026, 10, 20), date(2026, 10, 20), date(2026, 10, 20), date(2026, 10, 21)])
    budget = DailyBudget(limit_usd=1, today=lambda: next(days))
    budget.charge(1)
    assert not budget.allows_call()
    assert budget.allows_call()


def test_concurrency_limit_refuses_immediately():
    from llm.guard import ConcurrencyGate
    gate = ConcurrencyGate(limit=1)
    assert gate.try_enter()
    client, provider = make([VALID], gate=gate)
    result = call(client)
    assert result.error == 'busy' and provider.calls == []
    gate.leave()
    assert call(client).error is None


def test_provider_error_is_retried_once():
    from llm.base import ProviderError
    client, _ = make([ProviderError('逾時'), VALID])
    assert call(client).error is None
    client, provider = make([ProviderError('逾時'), TimeoutError('read timed out')])
    result = call(client)
    assert result.error == 'provider_error' and len(provider.calls) == 2 and result.input_tokens == 0


def test_question_limits_and_history_trim(monkeypatch):
    from llm.guard import check_question, trim_history
    monkeypatch.setenv('CHAT_MAX_INPUT_CHARS', '10')
    assert check_question('條紋怎麼辦') is None
    assert '上限' in check_question('條' * 11) and check_question('   ')
    history = [{'role': 'user' if i % 2 == 0 else 'assistant', 'content': str(i)} for i in range(20)]
    assert [m['content'] for m in trim_history(history)] == [str(i) for i in range(8, 20)]


def test_create_client_rejects_unknown_specs():
    from llm.client import create_client
    with pytest.raises(ValueError):
        create_client('no-provider', purpose='router')
    with pytest.raises(ValueError):
        create_client('mystery:model', purpose='router')


def test_anthropic_adapter_reads_text_blocks_and_charges_refusals():
    from llm.base import ProviderError
    from llm.providers.anthropic import AnthropicProvider

    class Stub:
        def __init__(self, response):
            self.response, self.params = response, None
            self.messages = self

        def with_options(self, **kwargs):
            return self

        def create(self, **params):
            self.params = params
            return self.response

    usage = SimpleNamespace(input_tokens=120, output_tokens=40)
    answer = SimpleNamespace(stop_reason='end_turn', usage=usage, content=[
        SimpleNamespace(type='thinking', thinking=''), SimpleNamespace(type='text', text=VALID)])
    stub = Stub(answer)
    reply = AnthropicProvider(client=stub).complete(model='m', system='s', messages=[], max_tokens=800,
                                                    timeout=30, config={'effort': 'low'})
    assert reply.text == VALID and (reply.input_tokens, reply.output_tokens) == (120, 40)
    assert 'temperature' not in stub.params and stub.params['output_config'] == {'effort': 'low'}

    refusal = SimpleNamespace(stop_reason='refusal', usage=usage, content=[])
    with pytest.raises(ProviderError) as caught:
        AnthropicProvider(client=Stub(refusal)).complete(model='m', system='s', messages=[], max_tokens=800,
                                                         timeout=30, config={})
    assert caught.value.input_tokens == 120



def test_openai_compatible_adapter_builds_request_and_reads_usage(monkeypatch):
    import io
    import json
    import urllib.request
    from llm.base import ProviderError
    from llm.providers.openai_compatible import OpenAICompatibleProvider
    sent = {}

    def fake_urlopen(request, timeout):
        sent['url'], sent['body'], sent['auth'] = request.full_url, json.loads(request.data), request.headers['Authorization']
        payload = {'choices': [{'message': {'content': VALID}}], 'usage': {'prompt_tokens': 90, 'completion_tokens': 30}}
        return io.BytesIO(json.dumps(payload).encode())

    monkeypatch.setattr(urllib.request, 'urlopen', fake_urlopen)
    provider = OpenAICompatibleProvider(api_key='test-key', base_url='https://example.test/v1/')
    reply = provider.complete(model='m', system='s', messages=[{'role': 'user', 'content': 'q'}], max_tokens=800,
                              timeout=30, config={'max_tokens_param': 'max_completion_tokens', 'json_mode': True,
                                                  'supports_temperature': False, 'reasoning_effort': 'low'})
    assert sent['url'] == 'https://example.test/v1/chat/completions' and sent['auth'] == 'Bearer test-key'
    assert sent['body']['messages'][0] == {'role': 'system', 'content': 's'}
    assert sent['body']['max_completion_tokens'] == 800 and 'temperature' not in sent['body']
    assert sent['body']['response_format'] == {'type': 'json_object'}
    assert sent['body']['reasoning_effort'] == 'low'
    assert (reply.text, reply.input_tokens, reply.output_tokens) == (VALID, 90, 30)
    monkeypatch.delenv('OPENAI_API_KEY', raising=False)
    with pytest.raises(ProviderError):
        OpenAICompatibleProvider().complete(model='m', system='s', messages=[], max_tokens=1, timeout=1, config={})
