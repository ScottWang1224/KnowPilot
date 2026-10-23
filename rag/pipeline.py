"""One conversation turn: route, then answer. Shared by chat_cli, the answer evaluation and (phase 4) the chat API."""
from dataclasses import dataclass, field

from llm import settings
from rag import answer, router
from rag.answer import AnswerResult
from rag.router import RouteResult


def clarify_text(clarify):
    """The assistant turn kept in history: the question and its numbered options."""
    return clarify.question + ''.join(f'\n{i}. {option}' for i, option in enumerate(clarify.options, 1))


@dataclass
class Conversation:
    history: list[dict] = field(default_factory=list)
    state: dict | None = None
    clarify_turns: int = 0

    def reset(self):
        self.history, self.state, self.clarify_turns = [], None, 0


@dataclass
class TurnResult:
    route: RouteResult
    answer: AnswerResult | None   # None while clarifying, or when no answer model is configured


def run_turn(db, user, text, conv, *, router_client=None, answer_client=None, use_glossary=True):
    """Route `text` within `conv`. A clarifying question extends the conversation; anything else
    is answered (when an answer model is available) and the conversation starts over."""
    route = router.route(db, user, text, history=conv.history, state=conv.state, clarify_turns=conv.clarify_turns,
                         client=router_client, use_glossary=use_glossary)
    if route.error == 'invalid_input':
        # Nothing was asked of the model; keep the conversation so the user can rephrase.
        return TurnResult(route, answer.answer(db, user, text, route))
    if route.action == 'clarify':
        conv.history += [{'role': 'user', 'content': text},
                         {'role': 'assistant', 'content': clarify_text(route.decision.clarify)}]
        conv.state, conv.clarify_turns = route.decision.state, conv.clarify_turns + 1
        return TurnResult(route, None)
    result = None
    if answer_client is not None or settings.answer_model():
        result = answer.answer(db, user, text, route, history=conv.history, client=answer_client)
    conv.reset()
    return TurnResult(route, result)
