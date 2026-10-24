"""One conversation turn: route, then answer. Shared by chat_cli, the evaluation scripts and the chat API."""
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


def history_text(turn):
    """What the assistant turn leaves in history, or None when it stays out (errors, no answer, remember)."""
    if turn.route.action == 'remember':
        return None
    if turn.route.action == 'clarify':
        return clarify_text(turn.route.decision.clarify)
    if turn.route.action == 'out_of_scope':
        return answer.OUT_OF_SCOPE_TEXT
    if turn.answer is None or turn.answer.output is None:
        return None
    return answer.compact_answer(turn.answer.output)


def run_turn(db, user, text, conv, *, router_client=None, answer_client=None, use_glossary=True,
             keep_history_after_answer=False, memory=None):
    """Route `text` within `conv`. A clarifying question extends the conversation; anything else
    is answered (when an answer model is available).

    After an answer the conversation starts over, unless keep_history_after_answer (the chat API):
    then the exchange stays in history for follow-up questions and only state and clarify_turns
    reset. A turn that ends in an error, or only asks to remember something, leaves the
    conversation as it was, so a request to remember in the middle of clarifying does not
    change what the router sees next.

    memory: the user's validated rag.memory.Memory (chat API); None leaves memory out of both prompts.
    """
    route = router.route(db, user, text, history=conv.history, state=conv.state, clarify_turns=conv.clarify_turns,
                         client=router_client, use_glossary=use_glossary,
                         memory=None if memory is None else memory.context)
    if route.error == 'invalid_input':
        # Nothing was asked of the model; keep the conversation so the user can rephrase.
        return TurnResult(route, answer.answer(db, user, text, route))
    if route.action == 'remember':
        return TurnResult(route, None)
    if route.action == 'clarify':
        conv.history += [{'role': 'user', 'content': text},
                         {'role': 'assistant', 'content': clarify_text(route.decision.clarify)}]
        conv.state, conv.clarify_turns = route.decision.state, conv.clarify_turns + 1
        return TurnResult(route, None)
    result = None
    if answer_client is not None or settings.answer_model():
        result = answer.answer(db, user, text, route, history=conv.history, client=answer_client,
                               preferences=None if memory is None else memory.preferences)
    turn = TurnResult(route, result)
    if not keep_history_after_answer:
        conv.reset()
    elif (reply := history_text(turn)) is not None:
        conv.history += [{'role': 'user', 'content': text}, {'role': 'assistant', 'content': reply}]
        conv.state, conv.clarify_turns = None, 0
    return turn
