"""Provider-neutral inference session contracts and events."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Protocol, TypeAlias


@dataclass(frozen=True, slots=True)
class UsageBreakdown:
    input_tokens: int = 0
    cached_input_tokens: int = 0
    cache_write_input_tokens: int = 0
    output_tokens: int = 0
    reasoning_output_tokens: int = 0
    total_tokens: int = 0

    @classmethod
    def from_object(cls, value: Any) -> UsageBreakdown:
        return cls(
            input_tokens=int(getattr(value, "input_tokens", 0) or 0),
            cached_input_tokens=int(getattr(value, "cached_input_tokens", 0) or 0),
            cache_write_input_tokens=int(getattr(value, "cache_write_input_tokens", 0) or 0),
            output_tokens=int(getattr(value, "output_tokens", 0) or 0),
            reasoning_output_tokens=int(getattr(value, "reasoning_output_tokens", 0) or 0),
            total_tokens=int(getattr(value, "total_tokens", 0) or 0),
        )

    def to_dict(self) -> dict[str, int]:
        return asdict(self)

    def __add__(self, other: UsageBreakdown) -> UsageBreakdown:
        return UsageBreakdown(
            **{
                field: getattr(self, field) + getattr(other, field)
                for field in self.__dataclass_fields__
            }
        )


@dataclass(frozen=True, slots=True)
class TextDelta:
    text: str


@dataclass(frozen=True, slots=True)
class ItemCompleted:
    kind: str
    item: dict[str, Any]


@dataclass(frozen=True, slots=True)
class UsageUpdated:
    last: UsageBreakdown
    cumulative: UsageBreakdown
    model_context_window: int | None = None


@dataclass(frozen=True, slots=True)
class InferenceError:
    message: str
    will_retry: bool = False


@dataclass(frozen=True, slots=True)
class TurnCompleted:
    status: str
    duration_ms: int | None
    error: str | None = None


@dataclass(frozen=True, slots=True)
class Notification:
    method: str


InferenceEvent: TypeAlias = (
    TextDelta | ItemCompleted | UsageUpdated | InferenceError | TurnCompleted | Notification
)


class ActiveTurn(Protocol):
    def stream(self) -> Iterator[InferenceEvent]: ...

    def steer(self, text: str) -> None: ...


class InferenceSession(Protocol):
    id: str
    model: str

    def turn(self, text: str, image_path: Path) -> ActiveTurn: ...
