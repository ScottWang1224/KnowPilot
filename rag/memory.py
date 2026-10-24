"""Agent memory items: which keys a user may remember and which values each key accepts.

Memory is structured only. Work-context values come from the clarify dimensions the user
can see (rag.router.memory_options), the answer style from a fixed list, and every write
and every read into a prompt is checked against them. So no stored value is free text, and
memory never names anything outside the user's own document access.
"""
from dataclasses import dataclass

CONTEXT_KEYS = ('line', 'product', 'shift', 'employment')   # clarify dimensions that stay the same between questions
STYLE_KEY = 'answer_style'
STYLE_VALUES = ('簡短', '詳細')
LABELS = {'line': '產線', 'product': '產品', 'shift': '班別', 'employment': '聘用類別', STYLE_KEY: '回答方式'}
NOTHING_TO_REMEMBER = '我只能記住以下資訊：產線、產品、班別、聘用類別、回答方式（簡短／詳細），而且要從可選的項目中選擇。'


@dataclass
class Memory:
    context: dict[str, str]       # work context for the router, e.g. {'line': 'CL-02'}
    preferences: dict[str, str]   # for the answer model: {'answer_style': '簡短'}

    def items(self):
        return {**self.context, **self.preferences}


def options_from(dimensions):
    """{key: [values]} available to a user whose visible dimensions are `dimensions`, in LABELS order."""
    options = {key: [str(v['value']) for v in dimensions[key]['values']] for key in CONTEXT_KEYS if key in dimensions}
    options[STYLE_KEY] = list(STYLE_VALUES)
    return options


def is_valid(options, key, value):
    return key in options and value in options[key]


def split(stored, options):
    """Stored {key: value} → Memory of the values still valid; invalid ones never reach a prompt."""
    valid = {k: v for k, v in stored.items() if is_valid(options, k, v)}
    return Memory({k: v for k, v in valid.items() if k in CONTEXT_KEYS},
                  {k: v for k, v in valid.items() if k == STYLE_KEY})


def described(key, value):
    return {'key': key, 'label': LABELS[key], 'value': value}


def format_options(options):
    """key（label）：值1、值2…, one key per line (the router's <memory_options>)."""
    return '\n'.join(f'{key}（{LABELS[key]}）：' + '、'.join(values) for key, values in options.items())
