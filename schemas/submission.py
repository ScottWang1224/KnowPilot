"""Submission request bodies. Only sizes are capped here (a broken body is a 400); the content rules
(lengths, category, a ## heading) are checked in routers/submissions.py and answered with 422."""
from typing import Literal

from pydantic import BaseModel, Field

HARD_LIMIT = 300_000   # well above the 100,000-character content rule, so that rule answers with its own message


class SubmissionFields(BaseModel):
    title: str = Field(max_length=1000)
    summary: str = Field(max_length=5000)
    content: str = Field(max_length=HARD_LIMIT)
    category: str = Field(max_length=64)
    visibility: Literal['company', 'department']
    change_note: str | None = Field(default=None, max_length=5000)
    version_bump: Literal['minor', 'major'] | None = None


class SubmissionCreate(SubmissionFields):
    # The department always comes from the signed-in user; a department field in the body is ignored.
    kind: Literal['create', 'revise']
    document_id: int | None = Field(default=None, ge=1)


class ReviewNote(BaseModel):
    review_note: str = Field(default='', max_length=1000)
