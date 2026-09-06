"""Scripted stand-in for the Gemini client.

Nano asks Gemini for three different things, so the fake can script three shapes:
a function call (intent classification), a JSON blob (receipt extraction and item
canonicalization), and plain text. Calls are recorded so tests can assert on what
was actually sent — including whether conversation history made it into the prompt.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, ClassVar


class FakeFunctionCall:
    def __init__(self, name: str, args: dict[str, Any]):
        self.name = name
        self.args = args


class FakePart:
    def __init__(self, text: str | None = None, function_call: FakeFunctionCall | None = None):
        self.text = text
        self.function_call = function_call


class FakeContent:
    def __init__(self, parts: list[FakePart]):
        self.parts = parts


class FakeCandidate:
    def __init__(self, parts: list[FakePart], finish_reason: str = "STOP"):
        self.content = FakeContent(parts)
        self.finish_reason = finish_reason


class FakeResponse:
    def __init__(self, parts: list[FakePart], finish_reason: str = "STOP"):
        self.candidates = [FakeCandidate(parts, finish_reason)]

    @property
    def text(self) -> str:
        joined = "".join(p.text or "" for p in self.candidates[0].content.parts)
        if not joined:
            raise ValueError("no text part in response")
        return joined


class EmptyResponse:
    """A response with no candidates — models really do return this on a safety block."""

    candidates: ClassVar[list[FakeCandidate]] = []

    @property
    def text(self) -> str:
        raise ValueError("no candidates")


@dataclass
class RecordedCall:
    prompt: str
    parts: list[Any] = field(default_factory=list)
    kwargs: dict[str, Any] = field(default_factory=dict)


class FakeGeminiModel:
    """Returns queued responses in order; the last one repeats once exhausted."""

    def __init__(self, model_name: str = "fake-flash"):
        self.model_name = model_name
        self.calls: list[RecordedCall] = []
        self._queue: list[Any] = []
        self._error: Exception | None = None

    # --- scripting ----------------------------------------------------------

    def queue_function_call(self, name: str, **args: Any) -> FakeGeminiModel:
        self._queue.append(FakeResponse([FakePart(function_call=FakeFunctionCall(name, args))]))
        return self

    def queue_json(self, payload: Any) -> FakeGeminiModel:
        self._queue.append(FakeResponse([FakePart(text=json.dumps(payload))]))
        return self

    def queue_text(self, text: str) -> FakeGeminiModel:
        self._queue.append(FakeResponse([FakePart(text=text)]))
        return self

    def queue_empty(self) -> FakeGeminiModel:
        """Simulate a safety block / empty candidate list."""
        self._queue.append(EmptyResponse())
        return self

    def raises(self, error: Exception) -> FakeGeminiModel:
        self._error = error
        return self

    # --- client surface -----------------------------------------------------

    def generate_content(self, contents: Any, **kwargs: Any) -> Any:
        return self._respond(contents, kwargs)

    def start_chat(self, **kwargs: Any) -> FakeChat:
        return FakeChat(self)

    def _respond(self, contents: Any, kwargs: dict[str, Any]) -> Any:
        parts = contents if isinstance(contents, list) else [contents]
        prompt = " ".join(p for p in parts if isinstance(p, str))
        self.calls.append(RecordedCall(prompt=prompt, parts=list(parts), kwargs=dict(kwargs)))

        if self._error is not None:
            raise self._error
        if not self._queue:
            raise AssertionError(
                f"FakeGeminiModel got an unscripted call ({len(self.calls)} so far). "
                "Queue a response with queue_function_call/queue_json/queue_text."
            )
        return self._queue.pop(0) if len(self._queue) > 1 else self._queue[0]

    # --- assertion helpers --------------------------------------------------

    @property
    def last_prompt(self) -> str:
        return self.calls[-1].prompt

    @property
    def call_count(self) -> int:
        return len(self.calls)


class FakeChat:
    def __init__(self, model: FakeGeminiModel):
        self._model = model
        self.history: list[str] = []

    def send_message(self, message: Any, **kwargs: Any) -> Any:
        self.history.append(message if isinstance(message, str) else str(message))
        return self._model._respond(message, kwargs)
