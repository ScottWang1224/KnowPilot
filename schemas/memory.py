"""Memory request bodies. Values are checked against rag.memory in the router; these only cap sizes."""
from pydantic import BaseModel, Field


class MemoryValue(BaseModel):
    value: str = Field(max_length=64)


class ConfirmRequest(BaseModel):
    # Anything else in the body (key, value) is ignored: the proposal is read from the stored message.
    message_id: int = Field(ge=1)
