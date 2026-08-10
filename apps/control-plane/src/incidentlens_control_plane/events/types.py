from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class RuntimeEventType(StrEnum):
    PROJECT_CREATED = "project.created"
    PROJECT_UPDATED = "project.updated"
    PROJECT_DELETED = "project.deleted"


# JsonValue is a union type for JSON-serializable values
# Using Any to avoid recursion issues with Pydantic
JsonValue = str | int | float | bool | None | list[Any] | dict[str, Any]


class RuntimeEvent(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    event_id: str = Field(min_length=1, max_length=120)
    sequence: int = Field(default=0, ge=0)
    event_type: RuntimeEventType
    occurred_at: datetime
    payload: dict[str, JsonValue]
