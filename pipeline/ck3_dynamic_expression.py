from __future__ import annotations

from dataclasses import dataclass
from typing import Iterator


@dataclass(frozen=True)
class ExpressionSpan:
    """A balanced CK3 expression, including its square brackets."""

    text: str
    start_index: int
    end_index: int

    def group(self, index: int = 0) -> str:
        if index != 0:
            raise IndexError(index)
        return self.text

    def start(self) -> int:
        return self.start_index

    def end(self) -> int:
        return self.end_index


@dataclass(frozen=True)
class StringLiteralSpan:
    """A quoted literal inside a CK3 expression."""

    value: str
    quote: str
    start_index: int
    end_index: int


def iter_expression_spans(text: str) -> Iterator[ExpressionSpan]:
    """Yield top-level balanced bracket expressions, respecting quotes and escapes."""

    index = 0
    while index < len(text):
        if text[index] != "[":
            index += 1
            continue

        start = index
        depth = 1
        quote = ""
        escaped = False
        index += 1
        while index < len(text):
            char = text[index]
            if quote:
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == quote:
                    quote = ""
            elif char in {"'", '"'}:
                quote = char
            elif char == "[":
                depth += 1
            elif char == "]":
                depth -= 1
                if depth == 0:
                    index += 1
                    yield ExpressionSpan(text[start:index], start, index)
                    break
            index += 1
        else:
            # Preserve malformed/unclosed input as plain text. Validation remains
            # responsible for reporting it; the repair provider must not guess.
            return


def iter_string_literal_spans(expression: str) -> Iterator[StringLiteralSpan]:
    """Yield complete quoted literals, including values containing escaped quotes."""

    index = 0
    while index < len(expression):
        quote = expression[index]
        if quote not in {"'", '"'}:
            index += 1
            continue

        start = index
        index += 1
        value_start = index
        escaped = False
        while index < len(expression):
            char = expression[index]
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                yield StringLiteralSpan(
                    expression[value_start:index],
                    quote,
                    start,
                    index + 1,
                )
                index += 1
                break
            index += 1
        else:
            return
