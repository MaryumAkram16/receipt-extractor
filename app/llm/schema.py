"""Input/output schemas for the /extract endpoint.

Model output is untrusted input, exactly like data arriving from an external
API. Everything here exists to reject anything that doesn't match, before it
ever reaches a caller.
"""
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field, field_validator


class ExtractRequest(BaseModel):
    text: str = Field(..., min_length=1, max_length=4000)

    @field_validator("text")
    @classmethod
    def not_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("text must not be blank")
        return v


class Currency(str, Enum):
    PKR = "PKR"
    USD = "USD"
    EUR = "EUR"
    GBP = "GBP"
    OTHER = "other"


class ExtractResult(BaseModel):
    vendor: Optional[str] = None
    date: Optional[str] = None
    total_amount: Optional[float] = Field(default=None, ge=0)
    currency: Optional[Currency] = None
    confidence: float = Field(..., ge=0.0, le=1.0)
    needs_review: bool

    @field_validator("date")
    @classmethod
    def date_shape(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return v
        import re

        if not re.match(r"^\d{4}-\d{2}-\d{2}$", v):
            raise ValueError("date must be YYYY-MM-DD or null")
        return v
