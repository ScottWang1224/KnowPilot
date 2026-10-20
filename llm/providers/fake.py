"""Scripted provider for tests and offline runs. Never touches the network."""
from llm.base import ProviderReply


class FakeProvider:
    """Replies come from a script, one item per call:

    - str: reply text (100 input tokens, 50 output tokens)
    - ProviderReply: reply with explicit token counts
    - Exception instance: raised, to simulate timeouts or HTTP errors
    """
    name = 'fake'

    def __init__(self, script=None):
        self.script = list(script or [])
        self.calls = []

    def complete(self, *, model, system, messages, max_tokens, timeout, config):
        self.calls.append({'model': model, 'system': system, 'messages': messages, 'max_tokens': max_tokens})
        if not self.script:
            raise AssertionError('FakeProvider script is empty')
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        if isinstance(item, ProviderReply):
            return item
        return ProviderReply(text=item, input_tokens=100, output_tokens=50)
