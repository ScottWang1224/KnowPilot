"""Chat request bodies. Unknown fields (history, state, department_id, role) are ignored, never used:
history and routing state always come from the database. Ignoring rather than rejecting them is
deliberate (auth-reference acceptance case 8), so the models keep pydantic's default extra='ignore'."""
from typing import Literal

from pydantic import BaseModel, Field, field_validator

# check_question() applies CHAT_MAX_INPUT_CHARS with a Chinese message; this only caps the body size.
MESSAGE_HARD_LIMIT = 10000


class ChatRequest(BaseModel):
    conversation_id: int | None = Field(default=None, ge=1)
    message: str = Field(max_length=MESSAGE_HARD_LIMIT)


class FeedbackRequest(BaseModel):
    rating: Literal['up', 'down']
    comment: str | None = Field(default=None, max_length=500)

    @field_validator('comment')
    @classmethod
    def blank_is_none(cls, value):
        if value is None:
            return None
        return value.strip() or None
