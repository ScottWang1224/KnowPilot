"""OpenAI-compatible Chat Completions over plain HTTP.

Works with OpenAI and other services that expose the same endpoint via OPENAI_BASE_URL.
Uses the standard library only, so no single vendor SDK is required.
"""
import json
import os
import urllib.error
import urllib.request

from llm.base import ProviderError, ProviderReply

DEFAULT_BASE_URL = 'https://api.openai.com/v1'


class OpenAICompatibleProvider:
    name = 'openai_compatible'

    def __init__(self, api_key=None, base_url=None):
        self.api_key = api_key or os.getenv('OPENAI_API_KEY', '')
        self.base_url = (base_url or os.getenv('OPENAI_BASE_URL') or DEFAULT_BASE_URL).rstrip('/')

    def complete(self, *, model, system, messages, max_tokens, timeout, config):
        if not self.api_key:
            raise ProviderError('OPENAI_API_KEY 未設定')
        body = {'model': model, 'messages': [{'role': 'system', 'content': system}] + messages,
                config.get('max_tokens_param', 'max_tokens'): max_tokens}
        if config.get('supports_temperature', True):
            body['temperature'] = 0
        if config.get('reasoning_effort'):
            body['reasoning_effort'] = config['reasoning_effort']
        if config.get('json_mode'):
            body['response_format'] = {'type': 'json_object'}
        request = urllib.request.Request(
            f'{self.base_url}/chat/completions', data=json.dumps(body).encode(), method='POST',
            headers={'Authorization': f'Bearer {self.api_key}', 'Content-Type': 'application/json'})
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                data = json.loads(response.read().decode())
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode(errors='replace')[:200]
            raise ProviderError(f'HTTP {exc.code}: {detail}') from None
        except (urllib.error.URLError, TimeoutError) as exc:
            raise ProviderError(f'連線失敗：{exc}') from None
        usage = data.get('usage') or {}
        text = (data.get('choices') or [{}])[0].get('message', {}).get('content') or ''
        return ProviderReply(text=text, input_tokens=usage.get('prompt_tokens', 0),
                             output_tokens=usage.get('completion_tokens', 0))
