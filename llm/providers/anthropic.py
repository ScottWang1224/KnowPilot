"""Anthropic Messages API through the official SDK.

Retries are handled once by llm.client, so the SDK's own retries are turned off.
Current Claude models reject `temperature`, so it is only sent when models.yaml enables it.
"""
import os

import anthropic

from llm.base import ProviderError, ProviderReply


class AnthropicProvider:
    name = 'anthropic'

    def __init__(self, client=None):
        self._client = client

    def _get_client(self, timeout):
        if self._client is None:
            # The SDK resolves credentials itself (ANTHROPIC_API_KEY, auth token or CLI profile).
            kwargs = {'api_key': os.environ['ANTHROPIC_API_KEY']} if os.getenv('ANTHROPIC_API_KEY') else {}
            self._client = anthropic.Anthropic(timeout=timeout, max_retries=0, **kwargs)
        return self._client

    def complete(self, *, model, system, messages, max_tokens, timeout, config):
        params = {'model': model, 'max_tokens': max_tokens, 'system': system, 'messages': messages}
        if config.get('supports_temperature', False):
            params['temperature'] = 0
        if config.get('effort'):
            params['output_config'] = {'effort': config['effort']}
        try:
            response = self._get_client(timeout).with_options(timeout=timeout).messages.create(**params)
        except anthropic.APITimeoutError:
            raise ProviderError('逾時') from None
        except anthropic.APIConnectionError:
            raise ProviderError('連線失敗') from None
        except anthropic.APIStatusError as exc:
            raise ProviderError(f'HTTP {exc.status_code}: {exc.message[:200]}') from None
        usage = response.usage
        if response.stop_reason == 'refusal':
            raise ProviderError('模型拒絕回答（refusal）', input_tokens=usage.input_tokens,
                                output_tokens=usage.output_tokens)
        # Thinking blocks may precede the answer; only text blocks carry the JSON.
        text = ''.join(block.text for block in response.content if block.type == 'text')
        return ProviderReply(text=text, input_tokens=usage.input_tokens, output_tokens=usage.output_tokens)
