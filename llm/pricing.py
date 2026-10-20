"""Per-model settings and prices from llm/models.yaml. Prices are maintained by the project owner."""
from pathlib import Path

import yaml

from llm import settings

MODELS_FILE = Path(__file__).with_name('models.yaml')


def load_models(path=MODELS_FILE):
    if not path.exists():
        return {}
    return (yaml.safe_load(path.read_text(encoding='utf-8')) or {}).get('models') or {}


class Pricing:
    """Looks up a model's config by its 'provider:model-id' spec."""

    def __init__(self, models=None):
        self.models = load_models() if models is None else models

    def config(self, spec):
        return self.models.get(spec) or {}

    def actual_cost(self, spec, input_tokens, output_tokens):
        """Cost from configured prices, or None when either price is missing."""
        cfg = self.config(spec)
        price_in, price_out = cfg.get('input_per_mtok'), cfg.get('output_per_mtok')
        if price_in is None or price_out is None:
            return None
        return (input_tokens * float(price_in) + output_tokens * float(price_out)) / 1_000_000

    def budget_cost(self, spec, input_tokens, output_tokens):
        """Cost charged to the daily budget: actual cost, or a conservative estimate when unpriced."""
        actual = self.actual_cost(spec, input_tokens, output_tokens)
        if actual is not None:
            return actual
        return (input_tokens + output_tokens) * settings.fallback_price_per_mtok() / 1_000_000
