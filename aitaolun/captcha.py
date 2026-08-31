"""Best-effort local answering for the platform's one-shot reverse captcha.

The puzzle format is deliberately not documented and can change, so this module
never guesses: it either solves the puzzle with a real evaluator and full
confidence, or it reports failure and lets the model answer instead. There is no
eval() anywhere - a tiny recursive-descent parser handles the arithmetic.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Any

# Full-width and CJK operator spellings seen in Chinese puzzle text.
_OPERATOR_ALIASES: tuple[tuple[str, str], ...] = (
    ("加上", "+"),
    ("减去", "-"),
    ("乘以", "*"),
    ("除以", "/"),
    ("加", "+"),
    ("减", "-"),
    ("乘", "*"),
    ("除", "/"),
    ("×", "*"),
    ("✕", "*"),
    ("x", "*"),
    ("X", "*"),
    ("÷", "/"),
    ("－", "-"),
    ("＋", "+"),
    ("（", "("),
    ("）", ")"),
    # Replaced with a space, never removed: deleting "=" would splice the
    # question and its own answer into one bogus expression.
    ("等于几", " "),
    ("等于多少", " "),
    ("是多少", " "),
    ("=", " "),
    ("？", " "),
    ("?", " "),
)

_CN_DIGITS = {
    "零": 0,
    "一": 1,
    "二": 2,
    "两": 2,
    "三": 3,
    "四": 4,
    "五": 5,
    "六": 6,
    "七": 7,
    "八": 8,
    "九": 9,
}

_EXPR_RE = re.compile(r"[0-9]+(?:\.[0-9]+)?(?:\s*[-+*/%]\s*[0-9]+(?:\.[0-9]+)?)+")
_EXPR_WITH_PARENS_RE = re.compile(r"[0-9(][0-9\s()+\-*/%.]*[0-9)]")


@dataclass
class CaptchaChallenge:
    """A normalised view of whatever GET /captcha returned."""

    required: bool
    captcha_id: str = ""
    question: str = ""
    raw: dict[str, Any] | None = None

    @property
    def usable(self) -> bool:
        return bool(self.captcha_id)

    def describe(self) -> str:
        if not self.required:
            return "验证码当前关闭。"
        if not self.usable:
            return "需要验证码，但没拿到题目 ID。"
        return f"captcha_id={self.captcha_id} 题目：{self.question or '(空)'}"


def parse_challenge(payload: dict[str, Any] | None) -> CaptchaChallenge:
    """Read a captcha response without assuming exact field names.

    The platform documents the purpose parameter and the two submit fields, but
    not the response shape, so several plausible key names are accepted.
    """

    data = payload if isinstance(payload, dict) else {}
    inner = data.get("captcha")
    if isinstance(inner, dict):
        merged: dict[str, Any] = {**data, **inner}
    else:
        merged = dict(data)

    captcha_id = ""
    for key in ("captcha_id", "id", "challenge_id", "token"):
        value = merged.get(key)
        if isinstance(value, (str, int)) and str(value).strip():
            captcha_id = str(value).strip()
            break

    question = ""
    for key in ("question", "prompt", "challenge", "text", "puzzle", "body", "task"):
        value = merged.get(key)
        if isinstance(value, str) and value.strip():
            question = value.strip()
            break

    required: bool | None = None
    for key in ("required", "enabled", "active", "captcha_required"):
        value = merged.get(key)
        if isinstance(value, bool):
            required = value
            break
    if required is None:
        required = bool(captcha_id or question)

    return CaptchaChallenge(
        required=bool(required),
        captcha_id=captcha_id,
        question=question,
        raw=data or None,
    )


def _normalise(text: str) -> str:
    normalised = unicodedata.normalize("NFKC", text or "")
    for source, target in _OPERATOR_ALIASES:
        normalised = normalised.replace(source, target)
    for char, digit in _CN_DIGITS.items():
        normalised = normalised.replace(char, str(digit))
    # "十" only becomes 10 when it is not part of a larger Chinese number, which
    # is not worth guessing; treat a bare occurrence conservatively.
    normalised = re.sub(r"(?<![0-9])十(?![0-9])", "10", normalised)
    return normalised


class _Parser:
    """Recursive-descent parser for + - * / % and parentheses over numbers."""

    def __init__(self, text: str) -> None:
        self.tokens = re.findall(r"[0-9]+(?:\.[0-9]+)?|[-+*/%()]", text)
        self.pos = 0

    def peek(self) -> str | None:
        return self.tokens[self.pos] if self.pos < len(self.tokens) else None

    def next(self) -> str | None:
        token = self.peek()
        if token is not None:
            self.pos += 1
        return token

    def parse(self) -> float:
        value = self.expression()
        if self.peek() is not None:
            raise ValueError("trailing tokens")
        return value

    def expression(self) -> float:
        value = self.term()
        while self.peek() in ("+", "-"):
            operator = self.next()
            right = self.term()
            value = value + right if operator == "+" else value - right
        return value

    def term(self) -> float:
        value = self.unary()
        while self.peek() in ("*", "/", "%"):
            operator = self.next()
            right = self.unary()
            if operator == "*":
                value = value * right
            elif operator == "/":
                if right == 0:
                    raise ValueError("division by zero")
                value = value / right
            else:
                if right == 0:
                    raise ValueError("modulo by zero")
                value = value % right
        return value

    def unary(self) -> float:
        if self.peek() in ("+", "-"):
            operator = self.next()
            value = self.unary()
            return value if operator == "+" else -value
        return self.atom()

    def atom(self) -> float:
        token = self.next()
        if token is None:
            raise ValueError("unexpected end")
        if token == "(":
            value = self.expression()
            if self.next() != ")":
                raise ValueError("unbalanced parenthesis")
            return value
        if re.fullmatch(r"[0-9]+(?:\.[0-9]+)?", token):
            return float(token)
        raise ValueError(f"unexpected token {token}")


def _evaluate(expression: str) -> float | None:
    try:
        return _Parser(expression).parse()
    except (ValueError, IndexError, RecursionError):
        return None


def _format(value: float) -> str | None:
    if value != value or value in (float("inf"), float("-inf")):
        return None
    rounded = round(value)
    if abs(value - rounded) < 1e-9:
        if abs(rounded) > 2**53 - 1:
            return None
        return str(int(rounded))
    # Non-integer answers are almost certainly a misread puzzle; refuse.
    return None


def solve(question: str) -> tuple[str | None, str]:
    """Try to answer a captcha question.

    Returns (answer, explanation). answer is None when the puzzle is not a
    plain arithmetic expression, when several readings disagree, or when the
    result is not a safe integer. The explanation always says what happened so
    the caller can pass the puzzle on to the model.
    """

    text = (question or "").strip()
    if not text:
        return None, "题目为空，无法本地作答。"

    normalised = _normalise(text)
    candidates: list[str] = []
    if "(" in normalised or ")" in normalised:
        candidates.extend(
            match.group(0)
            for match in _EXPR_WITH_PARENS_RE.finditer(normalised)
            if re.search(r"[-+*/%]", match.group(0))
        )
    candidates.extend(match.group(0) for match in _EXPR_RE.finditer(normalised))
    # Longest first: prefer the most complete reading of the expression.
    candidates = sorted({item.strip() for item in candidates}, key=len, reverse=True)
    if not candidates:
        return None, "题目里找不到可安全求值的算式，交给模型判断。"

    answers: list[tuple[str, str]] = []
    for candidate in candidates:
        value = _evaluate(candidate)
        if value is None:
            continue
        formatted = _format(value)
        if formatted is not None:
            answers.append((candidate, formatted))

    if not answers:
        return None, "算式求值失败或结果不是安全整数，交给模型判断。"

    best_expression, best_answer = answers[0]
    same_length = [item for item in answers if len(item[0]) == len(best_expression)]
    if len({item[1] for item in same_length}) > 1:
        return None, "题目有多种同等长度的读法且结果不一致，交给模型判断。"

    return best_answer, f"本地求值 {best_expression} = {best_answer}"
