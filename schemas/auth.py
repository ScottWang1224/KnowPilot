"""Request bodies. Unknown fields such as role are ignored, never stored."""
from pydantic import BaseModel, EmailStr, Field, field_validator


class RegisterRequest(BaseModel):
    name: str = Field(min_length=1, max_length=50)
    email: EmailStr = Field(max_length=254)
    password: str = Field(min_length=8, max_length=128)
    department_id: int = Field(ge=1)

    @field_validator('name')
    @classmethod
    def strip_name(cls, value):
        value = value.strip()
        if not value:
            raise ValueError('name is blank')
        return value

    @field_validator('email')
    @classmethod
    def normalize_email(cls, value):
        return value.lower()


class LoginRequest(BaseModel):
    # Plain strings: a malformed email is just a failed login (401), not a 400.
    email: str = Field(max_length=254)
    password: str = Field(max_length=128)


class DepartmentChangeRequest(BaseModel):
    department_id: int = Field(ge=1)
    confirm_clear_history: bool = False
