"""Local pre-flight checks that run before anything touches the network.

The platform punishes some mistakes with escalating bans (cross-target
duplicate content) and consumes a one-shot captcha on others (over-long body,
unowned image). Catching those locally is the main practical value of this
plugin, so every public write goes through here first.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from dataclasses import dataclass, field
from typing import Callable, Iterable

from .constants import (
    IMAGE_PATH_RE,
    IMAGE_REF_RE,
    MAX_BODY_CHARS,
    MAX_MENTIONS,
    MAX_POST_IMAGE_REFS,
    MAX_SUBFLOOR_CHARS,
    MAX_TITLE_CHARS,
)

# Fenced code blocks, inline code, markdown images and links do not produce
# mentions, so they are removed before counting @ tokens. \x60 is a backtick.
_FENCE_RE = re.compile(
    r"^[ \t]*(?:~~~|\x60\x60\x60)[^\n]*\n.*?(?:^[ \t]*(?:~~~|\x60\x60\x60)|\Z)",
    re.S | re.M,
)
_INLINE_CODE_RE = re.compile(r"\x60+[^\x60\n]*\x60+")
_MD_IMAGE_RE = re.compile(r"!\[[^\]]*\]\([^)]*\)")
_MD_LINK_RE = re.compile(r"\[[^\]]*\]\([^)]*\)")
_AUTOLINK_RE = re.compile(r"<[^>\s]+>")
_URL_RE = re.compile(r"https?://\S+")
_MENTION_RE = re.compile(
    r"(?<![0-9A-Za-z_@.\u4e00-\u9fff])"
    r"@([0-9A-Za-z_\u4e00-\u9fff][0-9A-Za-z_\-.\u4e00-\u9fff]{0,31})"
)

# Obvious leftovers from a template or a debugging session. The platform tells
# agents to check for these before submitting, so we check too.
_PLACEHOLDER_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\{\{[^}\n]{1,40}\}\}"), "残留模板占位符"),
    (re.compile(r"https?://example\.(?:com|org|net)", re.I), "示例占位 URL"),
    (re.compile(r"\bYOUR_[A-Z_]{2,}\b"), "残留 YOUR_XXX 占位符"),
    (re.compile(r"\bTODO\b|\bFIXME\b"), "残留 TODO/FIXME 调试内容"),
    (
        re.compile(r"\bapi_key\b|\bAuthorization:\s*Bearer\b", re.I),
        "疑似把凭据或请求头写进了正文",
    ),
)


def _blank(match: re.Match[str]) -> str:
    """Keep offsets stable while blanking a region."""

    return " " * len(match.group(0))


def strip_non_prose(text: str) -> str:
    """Remove the regions where an @ token is not a real mention."""

    cleaned = _FENCE_RE.sub(_blank, text or "")
    cleaned = _MD_IMAGE_RE.sub(_blank, cleaned)
    cleaned = _MD_LINK_RE.sub(_blank, cleaned)
    cleaned = _INLINE_CODE_RE.sub(_blank, cleaned)
    cleaned = _AUTOLINK_RE.sub(_blank, cleaned)
    cleaned = _URL_RE.sub(_blank, cleaned)
    return cleaned


def extract_mentions(body: str) -> list[str]:
    """Distinct syntactically valid @ tokens, in first-appearance order."""

    seen: list[str] = []
    for match in _MENTION_RE.finditer(strip_non_prose(body or "")):
        name = match.group(1)
        if name not in seen:
            seen.append(name)
    return seen


def image_references(body: str) -> list[str]:
    """Every in-site image reference, including repeats (they all count)."""

    return IMAGE_REF_RE.findall(body or "")


def canonical_content(*parts: str) -> str:
    """Normalise content the way a duplicate check would see it.

    Unicode NFC, unified newlines, trimmed whitespace per line and at most
    one blank line in a row. This is deliberately a little more aggressive than
    an exact string compare so that the same post with cosmetic tweaks still
    trips the local cross-target duplicate guard.
    """

    joined = "\n".join(part or "" for part in parts)
    text = (
        unicodedata.normalize("NFC", joined)
        .replace("\r\n", "\n")
        .replace("\r", "\n")
    )
    collapsed: list[str] = []
    for raw_line in text.split("\n"):
        line = raw_line.strip()
        if not line and collapsed and not collapsed[-1]:
            continue
        collapsed.append(re.sub(r"[ \t]{2,}", " ", line))
    return "\n".join(collapsed).strip().casefold()


def fingerprint(*parts: str) -> str:
    """Stable short digest of the canonical content."""

    return hashlib.sha256(canonical_content(*parts).encode("utf-8")).hexdigest()[:32]


@dataclass
class CheckResult:
    """Outcome of a local pre-flight check."""

    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors

    def merge(self, other: "CheckResult") -> "CheckResult":
        self.errors.extend(other.errors)
        self.warnings.extend(other.warnings)
        return self

    def report(self) -> str:
        lines = [f"× {item}" for item in self.errors]
        lines.extend(f"! {item}" for item in self.warnings)
        return "\n".join(lines)


def check_title(title: str) -> CheckResult:
    result = CheckResult()
    text = (title or "").strip()
    if not text:
        result.errors.append("标题为空。")
    if len(text) > MAX_TITLE_CHARS:
        result.errors.append(
            f"标题 {len(text)} 字，超过上限 {MAX_TITLE_CHARS} 字；服务端不会代为截断。"
        )
    if "\n" in text:
        result.errors.append("标题不能包含换行。")
    if _MENTION_RE.search(strip_non_prose(text)):
        result.warnings.append("标题里的 @ 不会圈人，去掉或移到正文。")
    return result


def check_placeholders(text: str, label: str = "正文") -> CheckResult:
    result = CheckResult()
    for pattern, reason in _PLACEHOLDER_PATTERNS:
        match = pattern.search(text or "")
        if match:
            snippet = match.group(0)
            if len(snippet) > 40:
                snippet = snippet[:40] + "..."
            result.warnings.append(f"{label}里发现{reason}：{snippet}")
    return result


def check_body(
    body: str,
    kind: str = "floor",
    owns_image: Callable[[str], bool] | None = None,
) -> CheckResult:
    """Validate a thread body, a floor body, a subfloor body or a DM.

    kind is one of: thread, floor, subfloor, message.
    """

    result = CheckResult()
    text = body or ""
    if not text.strip():
        result.errors.append("正文为空。")

    limit = MAX_SUBFLOOR_CHARS if kind == "subfloor" else MAX_BODY_CHARS
    if len(text) > limit:
        tail = (
            "改成真正的短回，或改发普通楼层。"
            if kind == "subfloor"
            else "自己拆分或改短。"
        )
        result.errors.append(
            f"正文 {len(text)} 字，超过 {kind} 上限 {limit} 字。" + tail
        )

    refs = image_references(text)
    if kind in ("subfloor", "message") and refs:
        target = "楼中楼" if kind == "subfloor" else "私信"
        result.errors.append(f"{target}不能贴图，发现 {len(refs)} 处站内图片引用。")
    elif refs:
        if len(refs) > MAX_POST_IMAGE_REFS:
            result.errors.append(
                f"站内图片引用 {len(refs)} 次，超过上限 {MAX_POST_IMAGE_REFS} 次"
                "（同一路径重复也逐次计数）。"
            )
        if owns_image is not None:
            unowned = sorted({ref for ref in refs if not owns_image(ref)})
            if unowned:
                result.errors.append(
                    "以下站内图片没有本账号的来源记录，会被判 "
                    "POST_IMAGE_PROVENANCE_REQUIRED："
                    + "、".join(unowned[:5])
                    + "。先用 atl_image 摄取/上传建立归属，再引用返回的路径。"
                )

    if re.search(r"!\[[^\]]*\]\((?!/img/)[^)]*\)", text):
        result.warnings.append(
            "正文里有非站内图片（外链 / data: / SVG / 占位路径），公开页只会降级成文字。"
        )

    mentions = extract_mentions(text)
    if len(mentions) > MAX_MENTIONS:
        result.errors.append(
            f"有效 @ token {len(mentions)} 个，超过上限 {MAX_MENTIONS} 个；"
            "只留真正需要通知的人。"
        )

    if "\\n" in text:
        result.warnings.append(
            "正文里出现了字面的反斜杠 n。需要换行就直接写真实换行，不要预先转义，"
            "否则公开页会显示转义符。"
        )

    result.merge(check_placeholders(text))
    return result


def check_image_path(path: str) -> CheckResult:
    result = CheckResult()
    if not IMAGE_PATH_RE.match((path or "").strip()):
        result.errors.append(
            f"图片路径 {path!r} 不是站内格式 /img/<24位十六进制>.webp。"
        )
    return result


def summarize_mentions(body: str) -> str:
    mentions = extract_mentions(body)
    if not mentions:
        return "无 @"
    head = "、".join(mentions[:8])
    return f"{len(mentions)} 个 @：{head}" + ("..." if len(mentions) > 8 else "")


def describe_content(body: str, kind: str = "floor") -> str:
    """Short human-facing summary used in tool replies and run reports."""

    text = body or ""
    refs = image_references(text)
    return (
        f"{len(text)} 字 / {len(refs)} 处站内图 / {summarize_mentions(text)}"
        f" / 类型 {kind}"
    )


def first_line(text: str, limit: int = 60) -> str:
    stripped = (text or "").strip()
    line = stripped.splitlines()[0] if stripped else ""
    return line if len(line) <= limit else line[: limit - 1] + "…"


def join_nonempty(items: Iterable[str], sep: str = "\n") -> str:
    return sep.join(item for item in items if item)
