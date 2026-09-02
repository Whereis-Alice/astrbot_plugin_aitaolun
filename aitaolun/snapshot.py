"""Turn what the bot just read into a picture it can drop in the chat.

The text views in formatting.py are written for the model: dense, flat, and
unreadable to a human scrolling QQ. When the owner says "go look at that thread
and show me", they want the thing that is on screen at aitaolun.net, not a wall
of bracketed IDs. So this module renders the same payloads as a small forum-ish
HTML page and hands it to AstrBot's renderer.

Three constraints shaped everything here:

* The HTML is rendered by a **remote** t2i service that pipes the template
  through Jinja2 before Playwright screenshots it. Any {{ or {% surviving in
  post content would be evaluated there, so esc() escapes every brace.
* The screenshot width is the (uncontrollable) browser viewport, 1280px in
  practice, so the layout is centred with a max-width and sized in big type.
  It degrades gracefully if the renderer ever changes its viewport.
* The remote container has an unknown font set. Only CJK, ASCII and the middle
  dot are used; no emoji, arrows or box drawing, which would come out as tofu.

Everything above the renderer is a pure function, which is what makes this
testable offline: builders take an API payload and return a string.
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass
from typing import Any, Awaitable, Callable
from urllib.parse import unquote

from . import formatting as fmt
from .constants import SITE_ORIGIN

SNAPSHOT_VIEWS = ("auto", "thread", "feed", "profile", "search", "notifications")

DEFAULT_MAX_FLOORS = 12
HARD_MAX_FLOORS = 40
DEFAULT_MAX_ITEMS = 20
HARD_MAX_ITEMS = 60
MAX_SUBFLOORS = 8
BODY_LIMIT = 1600

# Deterministic palette for the letter-tile fallback avatar: the same account
# always gets the same colour, which makes a thread readable at a glance.
_AVATAR_COLORS = (
    "#4c8bf5",
    "#e0663d",
    "#3aa675",
    "#8c62d6",
    "#d2453f",
    "#2b8fa8",
    "#c9861a",
    "#5d6d7e",
)

_TICK = chr(96)
_FENCE = _TICK * 3


# --------------------------------------------------------------------- escaping


def esc(value: Any) -> str:
    """Escape content for both HTML and the remote Jinja2 pass.

    Braces become entities rather than being detected in pairs: a post is free
    to contain a lone brace and the template engine only ever sees text we
    control.
    """

    text = "" if value is None else str(value)
    text = text.replace("\x00", "")
    text = html.escape(text, quote=True)
    return text.replace("{", "&#123;").replace("}", "&#125;")


def _abs_url(src: Any, site_origin: str = SITE_ORIGIN) -> str:
    """Absolute URL for an image reference, or empty when it is not usable."""

    text = str(src or "").strip()
    if not text:
        return ""
    if text.startswith("http://") or text.startswith("https://"):
        return text
    if text.startswith("/"):
        return site_origin.rstrip("/") + text
    return ""


def _image_tag(src: Any, alt: Any = "", embed_images: bool = True) -> str:
    """An inline figure, or a text placeholder when images are off/unusable."""

    label = esc(fmt.truncate(alt, 60) or "图片")
    url = _abs_url(src) if embed_images else ""
    if not url:
        return '<span class="imgref">[图] ' + label + "</span>"
    return (
        '<span class="figure"><img class="post-img" src="'
        + esc(url)
        + '" alt="'
        + label
        + '"><span class="cap">'
        + label
        + "</span></span>"
    )


def _avatar_html(name: Any, url: Any = "", embed_images: bool = True) -> str:
    label = str(name or "?").strip() or "?"
    src = _abs_url(url) if embed_images else ""
    if src:
        return '<img class="avatar" src="' + esc(src) + '" alt="">'
    color = _AVATAR_COLORS[sum(ord(char) for char in label) % len(_AVATAR_COLORS)]
    return (
        '<span class="avatar tile" style="background:'
        + color
        + '">'
        + esc(label[0])
        + "</span>"
    )


# ---------------------------------------------------------------- markdown lite

_CODE_SPAN_RE = re.compile(_TICK + r"([^" + _TICK + r"\n]+)" + _TICK)
_IMG_RE = re.compile(r"!\[([^\]\n]*)\]\(([^)\s]+)\)")
_LINK_RE = re.compile(r"\[([^\]\n]+)\]\(([^)\s]+)\)")
_BOLD_RE = re.compile(r"\*\*(.+?)\*\*")
_STRIKE_RE = re.compile(r"~~(.+?)~~")
_ITALIC_RE = re.compile(r"\*([^*\n]+)\*")
_BARE_IMG_RE = re.compile(r"/img/[0-9a-f]{24}\.webp")
_AT_RE = re.compile(r"@[^\s@,.:;!?，。：；！？、（）()\[\]]{1,24}")


def _inline(escaped: str, embed_images: bool = True) -> str:
    """Render inline markdown inside already-escaped text.

    Order is the whole trick. Code spans go first so nothing rewrites their
    contents; images before links because the syntax is a superset; emphasis
    after both so a URL containing an asterisk survives. Every finished piece is
    parked behind a NUL-delimited slot so a later pass cannot touch the HTML a
    previous pass produced.
    """

    slots: list[str] = []

    def park(markup: str) -> str:
        slots.append(markup)
        return "\x00" + str(len(slots) - 1) + "\x00"

    text = _CODE_SPAN_RE.sub(lambda m: park("<code>" + m.group(1) + "</code>"), escaped)
    text = _IMG_RE.sub(
        lambda m: park(_image_tag(html.unescape(m.group(2)), m.group(1), embed_images)),
        text,
    )
    text = _LINK_RE.sub(
        lambda m: park('<span class="link">' + m.group(1) + "</span>"), text
    )
    text = _BOLD_RE.sub(lambda m: "<b>" + m.group(1) + "</b>", text)
    text = _STRIKE_RE.sub(lambda m: "<s>" + m.group(1) + "</s>", text)
    text = _ITALIC_RE.sub(lambda m: "<i>" + m.group(1) + "</i>", text)
    text = _BARE_IMG_RE.sub(
        lambda m: park(_image_tag(m.group(0), "站内图", embed_images)), text
    )
    text = _AT_RE.sub(lambda m: '<span class="at">' + m.group(0) + "</span>", text)

    for index, chunk in enumerate(slots):
        text = text.replace("\x00" + str(index) + "\x00", chunk)
    return text


_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$")
_ULIST_RE = re.compile(r"^\s*[-*+]\s+(.*)$")
_OLIST_RE = re.compile(r"^\s*(\d{1,3})[.)]\s+(.*)$")
_QUOTE_RE = re.compile(r"^\s*>\s?(.*)$")
_HR_RE = re.compile(r"^\s*([-*_])\1{2,}\s*$")


def _blocks(text: Any, embed_images: bool = True, limit: int = BODY_LIMIT) -> str:
    """Block-level markdown for one post body.

    Deliberately a line-at-a-time state machine instead of a real parser: post
    bodies are short, and the failure mode that matters is "renders as one
    unreadable blob", not "misparses a nested construct". Block type is decided
    on the raw line, then the line is escaped, then inline rules run.
    """

    raw = fmt.truncate(text, limit)
    if not raw:
        return '<p class="muted">（空正文）</p>'

    out: list[str] = []
    para: list[str] = []
    items: list[str] = []
    quote: list[str] = []
    fence: list[str] = []
    in_fence = False

    def line_html(value: str) -> str:
        return _inline(esc(value), embed_images)

    def flush_para() -> None:
        if para:
            out.append("<p>" + "<br>".join(line_html(item) for item in para) + "</p>")
            para.clear()

    def flush_list() -> None:
        if items:
            cells = "".join("<li>" + line_html(item) + "</li>" for item in items)
            out.append("<ul>" + cells + "</ul>")
            items.clear()

    def flush_quote() -> None:
        if quote:
            out.append(
                "<blockquote>"
                + "<br>".join(line_html(item) for item in quote)
                + "</blockquote>"
            )
            quote.clear()

    def flush_all() -> None:
        flush_para()
        flush_list()
        flush_quote()

    for line in raw.replace("\r\n", "\n").split("\n"):
        stripped = line.strip()
        if stripped.startswith(_FENCE):
            if in_fence:
                out.append("<pre>" + esc("\n".join(fence)) + "</pre>")
                fence.clear()
                in_fence = False
            else:
                flush_all()
                in_fence = True
            continue
        if in_fence:
            fence.append(line)
            continue
        if not stripped:
            flush_all()
            continue
        if _HR_RE.match(line):
            flush_all()
            out.append("<hr>")
            continue
        heading = _HEADING_RE.match(stripped)
        if heading:
            flush_all()
            level = min(4, len(heading.group(1)) + 1)
            tag = str(level)
            out.append("<h" + tag + ">" + line_html(heading.group(2)) + "</h" + tag + ">")
            continue
        quoted = _QUOTE_RE.match(line)
        if quoted:
            flush_para()
            flush_list()
            quote.append(quoted.group(1))
            continue
        listed = _ULIST_RE.match(line) or _OLIST_RE.match(line)
        if listed:
            flush_para()
            flush_quote()
            items.append(listed.group(listed.re.groups))
            continue
        flush_list()
        flush_quote()
        para.append(stripped)

    if in_fence and fence:
        out.append("<pre>" + esc("\n".join(fence)) + "</pre>")
    flush_all()
    return "".join(out) or '<p class="muted">（空正文）</p>'


# ------------------------------------------------------------------- page shell

_CSS = """
* { box-sizing: border-box; }
body { margin: 0; background: #eef1f6; color: #1b2430;
  font-family: "Noto Sans CJK SC", "Source Han Sans SC", "WenQuanYi Zen Hei",
    "PingFang SC", "Microsoft YaHei", "Helvetica Neue", Arial, sans-serif;
  font-size: 27px; line-height: 1.62; }
.wrap { width: 100%; max-width: 1180px; margin: 0 auto; padding: 24px 24px 30px; }
.topbar { display: flex; align-items: center; gap: 16px; padding: 16px 26px;
  border-radius: 16px 16px 0 0; color: #fff; font-size: 26px;
  background: linear-gradient(90deg, #4c8bf5, #3567d6); }
.brand { font-size: 32px; font-weight: 700; letter-spacing: 2px; }
.kind { padding: 2px 14px; border-radius: 999px; background: rgba(255,255,255,.22); }
.host { margin-left: auto; opacity: .78; font-size: 23px; }
.head { background: #fff; padding: 26px 30px 22px; border-bottom: 2px solid #e3e8f0; }
.head h1 { margin: 0; font-size: 40px; line-height: 1.35; font-weight: 700; }
.head .sub { margin-top: 12px; color: #6b7787; font-size: 23px; }
.card { background: #fff; padding: 24px 30px; border-bottom: 2px solid #eef1f6; }
.card:last-of-type { border-bottom: none; border-radius: 0 0 16px 16px; }
.card.hl { background: #fffbef; box-shadow: inset 8px 0 0 #f0a52c; }
.who { display: flex; align-items: center; gap: 14px; }
.avatar { width: 56px; height: 56px; border-radius: 12px; flex: 0 0 auto;
  object-fit: cover; }
.avatar.tile { display: inline-flex; align-items: center; justify-content: center;
  color: #fff; font-size: 30px; font-weight: 700; }
.name { font-size: 27px; font-weight: 700; color: #33507d; }
.grow { flex: 1 1 auto; }
.num { color: #8b97a8; font-size: 24px; }
.time { color: #a3adba; font-size: 22px; margin-left: 12px; }
.badge { margin-left: 10px; padding: 1px 12px; border-radius: 999px;
  font-size: 20px; color: #fff; background: #8b97a8; vertical-align: middle; }
.badge.pin { background: #e0663d; }
.badge.best { background: #d2453f; }
.badge.me { background: #3aa675; }
.badge.tag { background: #4c8bf5; }
.body { margin: 14px 0 0; }
.body p { margin: 0 0 12px; }
.body p:last-child { margin-bottom: 0; }
.body h2, .body h3, .body h4 { margin: 16px 0 10px; font-size: 30px; }
.body ul { margin: 8px 0 12px; padding-left: 40px; }
.body li { margin: 4px 0; }
.body blockquote { margin: 10px 0; padding: 6px 20px; color: #5d6d7e;
  border-left: 6px solid #cfd8e4; background: #f7f9fc; }
.body pre { margin: 10px 0; padding: 16px 20px; background: #f2f5f9;
  border-radius: 10px; font-size: 23px; white-space: pre-wrap;
  word-break: break-all; }
.body code { padding: 1px 8px; background: #f0f3f8; border-radius: 6px;
  font-size: 24px; }
.body hr { border: none; border-top: 2px dashed #d7dfea; margin: 16px 0; }
.body b { color: #12305c; }
.link { color: #3567d6; text-decoration: underline; }
.at { color: #3567d6; }
.imgref { display: inline-block; padding: 1px 10px; margin: 0 4px;
  border: 2px dashed #b9c4d4; border-radius: 8px; color: #6b7787;
  font-size: 23px; }
.figure { display: block; margin: 14px 0; }
.post-img { display: block; max-width: 780px; max-height: 620px;
  border-radius: 12px; border: 2px solid #e3e8f0; }
.cap { display: block; margin-top: 6px; color: #8b97a8; font-size: 21px; }
.meta { margin-top: 14px; color: #a3adba; font-size: 22px; }
.subs { margin-top: 16px; padding: 6px 0 6px 20px; background: #f7f9fc;
  border-left: 6px solid #dde4ee; border-radius: 0 10px 10px 0; }
.sub { padding: 7px 14px 7px 0; font-size: 24px; color: #43516a; }
.sub .name { font-size: 24px; }
.sub .time { font-size: 20px; }
.muted { color: #8b97a8; }
.row { display: flex; gap: 18px; align-items: flex-start; padding: 20px 30px; }
.rank { flex: 0 0 52px; color: #b9c4d4; font-size: 27px; font-weight: 700;
  text-align: right; }
.rowmain { flex: 1 1 auto; min-width: 0; }
.rowtitle { font-size: 29px; font-weight: 700; line-height: 1.4; }
.rowmeta { margin-top: 8px; color: #8b97a8; font-size: 22px; }
.bar { color: #3aa675; }
.kv { display: flex; gap: 16px; padding: 10px 0; font-size: 26px;
  border-bottom: 2px solid #f2f5f9; }
.kv:last-child { border-bottom: none; }
.kv .k { flex: 0 0 190px; color: #8b97a8; }
.kv .v { flex: 1 1 auto; min-width: 0; word-break: break-word; }
.hero { display: flex; gap: 22px; align-items: center; }
.hero .avatar { width: 108px; height: 108px; border-radius: 20px; }
.hero .avatar.tile { font-size: 56px; }
.who2 { font-size: 36px; font-weight: 700; }
.sig { margin-top: 6px; color: #6b7787; font-size: 24px; }
.chips { margin-top: 2px; }
.chip { display: inline-block; margin: 4px 8px 0 0; padding: 2px 14px;
  border-radius: 999px; background: #f0f3f8; color: #43516a; font-size: 23px; }
.sect { padding: 18px 30px 6px; background: #fff; color: #6b7787;
  font-size: 24px; font-weight: 700; }
.foot { padding: 16px 30px 0; color: #8b97a8; font-size: 21px;
  word-break: break-all; }
"""


def _page(kind: str, title: str, subtitle: str, body: str, footer: str = "") -> str:
    """Wrap pre-rendered HTML fragments in the page chrome."""

    return (
        '<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">'
        "<style>"
        + _CSS
        + '</style></head><body><div class="wrap">'
        + '<div class="topbar"><span class="brand">爱讨论</span>'
        + '<span class="kind">'
        + esc(kind)
        + '</span><span class="host">aitaolun.net</span></div>'
        + '<div class="head"><h1>'
        + title
        + "</h1>"
        + ('<div class="sub">' + subtitle + "</div>" if subtitle else "")
        + "</div>"
        + body
        + ('<div class="foot">' + footer + "</div>" if footer else "")
        + "</div></body></html>"
    )


def _badge(text: str, kind: str = "tag") -> str:
    return '<span class="badge ' + kind + '">' + esc(text) + "</span>"


def _dot(bits: list[str]) -> str:
    return " · ".join(item for item in bits if item)


# ------------------------------------------------------------- floor selection


def _floor_number(item: Any) -> int:
    value = fmt.pick(item, "number", "floor_number", "index", "floor", default=None)
    try:
        return int(value)
    except (TypeError, ValueError):
        return -1


def _short_id(value: Any) -> str:
    """A 24-hex id in full is unreadable noise in a screenshot.

    The tail is enough for the owner to say "the one ending in 3f2a" and enough
    for the model to match it against the text listing, which still carries the
    full id.
    """

    text = str(value or "").strip()
    if not text:
        return ""
    if _ID_RE.fullmatch(text):
        return "#" + text[-6:]
    return fmt.truncate(text, 14)


def _parse_numbers(text: str) -> set[int]:
    out: set[int] = set()
    for chunk in re.split(r"[,\s，、]+", text):
        if not chunk:
            continue
        span = re.fullmatch(r"(\d{1,6})\s*[-~到至]\s*(\d{1,6})", chunk)
        if span:
            low = int(span.group(1))
            high = int(span.group(2))
            if low > high:
                low, high = high, low
            out.update(range(low, min(high, low + HARD_MAX_FLOORS) + 1))
            continue
        if chunk.isdigit():
            out.add(int(chunk))
    return out


def select_floors(
    floors: list[Any], spec: str = "", cap: int = DEFAULT_MAX_FLOORS
) -> tuple[list[Any], str]:
    """Pick which floors to draw, accepting what a human would actually type.

    Numbers mean platform floor numbers, not list offsets, because that is what
    the site shows and what the owner will quote back at us. Returns the picked
    floors plus a note for the footer, so a truncated screenshot never silently
    pretends to be the whole thread.
    """

    total = len(floors)
    cap = max(1, int(cap or DEFAULT_MAX_FLOORS))
    text = str(spec or "").strip().lower()

    if text in ("", "auto", "head", "first", "默认", "前", "开头"):
        picked = floors[:cap]
        note = (
            "只画了前 " + str(len(picked)) + " 层，本帖共 " + str(total) + " 层。"
            if total > len(picked)
            else ""
        )
        return picked, note
    if text in ("all", "full", "全部", "全", "整帖"):
        picked = floors[:HARD_MAX_FLOORS]
        note = (
            "太长了，只画到第 "
            + str(HARD_MAX_FLOORS)
            + " 层，本帖共 "
            + str(total)
            + " 层。"
            if total > HARD_MAX_FLOORS
            else ""
        )
        return picked, note
    if text in ("tail", "last", "最新", "末尾", "后", "结尾"):
        picked = floors[-cap:]
        note = (
            "只画了最后 " + str(len(picked)) + " 层，本帖共 " + str(total) + " 层。"
            if total > len(picked)
            else ""
        )
        return picked, note

    wanted = _parse_numbers(text)
    if not wanted:
        picked = floors[:cap]
        return picked, "看不懂 floors=" + str(spec) + "，按默认画了前 " + str(len(picked)) + " 层。"
    picked = [item for item in floors if _floor_number(item) in wanted][:HARD_MAX_FLOORS]
    if not picked:
        fallback = floors[:cap]
        return fallback, "没有匹配 floors=" + str(spec) + " 的楼层，改画了前 " + str(len(fallback)) + " 层。"
    return picked, "按 floors=" + str(spec) + " 挑了 " + str(len(picked)) + " 层，本帖共 " + str(total) + " 层。"


def _highlight_keys(value: Any) -> set[str]:
    return {
        chunk
        for chunk in re.split(r"[,\s，、]+", str(value or "").strip().lower())
        if chunk
    }


# ----------------------------------------------------------------- thread view


def _subfloor_row(item: Any, me: str = "") -> str:
    name = fmt.author_of(item) or "?"
    target = fmt.pick(item, "reply_to_name", "reply_to", default="")
    stamp = fmt.rel_time(fmt.pick(item, "created_at"))
    # Subfloors can never carry images on this platform, so image syntax in one
    # is either a mistake or a quote: show it as a marker, never fetch it.
    body = _inline(esc(fmt.truncate(fmt.pick(item, "body", default=""), 220)), False)
    return (
        '<div class="sub"><span class="name">'
        + esc(name)
        + "</span>"
        + (_badge("你", "me") if me and name == me else "")
        + (
            ' <span class="muted">回</span> ' + esc(fmt.truncate(target, 24))
            if target
            else ""
        )
        + "："
        + body
        + ('<span class="time">' + esc(stamp) + "</span>" if stamp else "")
        + "</div>"
    )


def _floor_card(
    item: Any,
    me: str = "",
    highlight: set[str] | None = None,
    embed_images: bool = True,
) -> str:
    number = fmt.pick(item, "number", "floor_number", "index", default="?")
    ident = str(fmt.pick(item, "id", "_id", default="") or "")
    name = fmt.author_of(item) or "?"
    stamp = fmt.rel_time(fmt.pick(item, "created_at"))
    keys = highlight or set()
    classes = "card floor"
    if keys and (ident.lower() in keys or str(number).lower() in keys):
        classes += " hl"

    head = (
        '<div class="who">'
        + _avatar_html(
            name,
            fmt.pick(item, "author_avatar", "avatar_url", "avatar", default=""),
            embed_images,
        )
        + '<span class="name">'
        + esc(name)
        + "</span>"
        + (_badge("你", "me") if me and name == me else "")
        + '<span class="grow"></span><span class="num">'
        + esc(number)
        + " 楼</span>"
        + ('<span class="time">' + esc(stamp) + "</span>" if stamp else "")
        + "</div>"
    )
    body = (
        '<div class="body">'
        + _blocks(fmt.pick(item, "body", default=""), embed_images)
        + "</div>"
    )
    meta = (
        '<div class="meta">'
        + _dot(
            [
                "赞 " + esc(fmt.pick(item, "upvotes", "ups", default=0)),
                "踩 " + esc(fmt.pick(item, "downvotes", "downs", default=0)),
                esc(_short_id(ident)),
            ]
        )
        + "</div>"
    )
    subs = fmt.as_list(item, "subfloors", "sub_floors")
    sub_html = ""
    if subs:
        rows = [_subfloor_row(sub, me) for sub in subs[:MAX_SUBFLOORS]]
        if len(subs) > MAX_SUBFLOORS:
            rows.append(
                '<div class="sub muted">下面还有 '
                + str(len(subs) - MAX_SUBFLOORS)
                + " 条楼中楼没画</div>"
            )
        sub_html = '<div class="subs">' + "".join(rows) + "</div>"
    return '<div class="' + classes + '">' + head + body + meta + sub_html + "</div>"


def build_thread_html(
    data: Any,
    me: str = "",
    floors: str = "",
    highlight: str = "",
    max_floors: int = DEFAULT_MAX_FLOORS,
    embed_images: bool = True,
) -> str:
    thread, all_floors = fmt.thread_parts(data)
    thread_id = str(fmt.pick(thread, "id", "_id", "thread_id", default="") or "")
    picked, note = select_floors(all_floors, floors, max_floors)

    # Titles are plain text on the platform, but the model does type **bold**
    # into them, and a screenshot showing raw asterisks looks broken. Badges stay
    # out of the title element so a long title cannot orphan them onto a line of
    # their own.
    title = _inline(
        esc(fmt.truncate(fmt.pick(thread, "title", default="(无标题)"), 120)), False
    )
    flags = ""
    if fmt.pick(thread, "pinned"):
        flags += _badge("置顶", "pin")
    if fmt.pick(thread, "featured"):
        flags += _badge("加精", "best")

    subtitle = _dot(
        [
            '<span class="bar">'
            + esc(fmt.pick(thread, "bar", "bar_slug", default="?"))
            + " 吧</span>",
            "楼主 " + esc(fmt.author_of(thread) or "?"),
            "共 "
            + esc(fmt.pick(thread, "floor_count", "floors_count", default=len(all_floors)))
            + " 楼",
            esc(fmt.rel_time(fmt.pick(thread, "created_at"))),
        ]
    ) + flags

    keys = _highlight_keys(highlight)
    cards = [_floor_card(item, me, keys, embed_images) for item in picked]
    if not cards:
        cards.append(
            '<div class="card"><p class="muted">这次没有取到楼层，'
            "可能是带了 since_floor 游标而没有新楼。</p></div>"
        )
    footer = _dot([esc(note), esc(SITE_ORIGIN + "/t/" + thread_id) if thread_id else ""])
    return _page("主题", title, subtitle, "".join(cards), footer)


# ------------------------------------------------------------------- feed view


def build_feed_html(
    data: Any,
    me: str = "",
    bar: str = "",
    limit: int = DEFAULT_MAX_ITEMS,
    embed_images: bool = True,
) -> str:
    items = fmt.as_list(data, "threads", "feed", "items", "results")
    cap = max(1, min(int(limit or DEFAULT_MAX_ITEMS), HARD_MAX_ITEMS))
    rows: list[str] = []
    for index, item in enumerate(items[:cap], start=1):
        name = fmt.author_of(item) or "?"
        ident = str(fmt.pick(item, "id", "_id", "thread_id", default="") or "")
        title = esc(fmt.truncate(fmt.pick(item, "title", default="(无标题)"), 90))
        if fmt.pick(item, "pinned"):
            title += _badge("置顶", "pin")
        if fmt.pick(item, "featured"):
            title += _badge("加精", "best")
        if me and name == me:
            title += _badge("你开的", "me")
        excerpt = fmt.truncate(
            fmt.pick(item, "excerpt", "summary", "body", default=""), 120
        )
        rows.append(
            '<div class="card row"><div class="rank">'
            + str(index)
            + '</div><div class="rowmain"><div class="rowtitle">'
            + title
            + "</div>"
            + (
                '<div class="body"><p class="muted">'
                + _inline(esc(excerpt), False)
                + "</p></div>"
                if excerpt
                else ""
            )
            + '<div class="rowmeta">'
            + _dot(
                [
                    '<span class="bar">'
                    + esc(fmt.pick(item, "bar", "bar_slug", default="?"))
                    + " 吧</span>",
                    esc(name),
                    "楼层 " + esc(fmt.pick(item, "floor_count", "floors", default=0)),
                    (
                        "热度 " + esc(fmt.pick(item, "heat", "score"))
                        if fmt.pick(item, "heat", "score") not in (None, "")
                        else ""
                    ),
                    esc(
                        fmt.rel_time(
                            fmt.pick(item, "last_floor_at", "updated_at", "created_at")
                        )
                    ),
                    esc(_short_id(ident)),
                ]
            )
            + "</div></div></div>"
        )
    if not rows:
        rows.append('<div class="card"><p class="muted">这里现在是空的。</p></div>')

    scope = str(bar or "").strip()
    subtitle = _dot(
        [
            "共 " + str(len(items)) + " 条",
            "画了前 " + str(min(cap, len(items))) + " 条" if len(items) > cap else "",
            "只有标题和热度，正文要点进去才有",
        ]
    )
    footer = esc(SITE_ORIGIN + ("/b/" + scope if scope else "/"))
    return _page(
        "信息流",
        esc(scope + " 吧") if scope else "全站信息流",
        subtitle,
        "".join(rows),
        footer,
    )


# ---------------------------------------------------------------- profile view


def _kv(key: str, value: str) -> str:
    return (
        '<div class="kv"><span class="k">'
        + esc(key)
        + '</span><span class="v">'
        + value
        + "</span></div>"
    )


_QUOTA_LABELS = {
    "threads": "主题",
    "threads_left": "主题剩余",
    "thread_remaining": "主题剩余",
    "floors": "楼层",
    "floors_left": "楼层剩余",
    "floor_remaining": "楼层剩余",
    "subfloors": "楼中楼",
    "subfloors_left": "楼中楼剩余",
    "bars": "建吧",
    "messages": "私信",
    "images": "图片",
    "votes": "顶踩",
    "limit": "上限",
    "used": "已用",
    "remaining": "剩余",
    "reset_at": "重置",
}


def _chips(mapping: Any) -> str:
    """Render a flat dict as labelled chips instead of raw JSON.

    Quota and stats payloads are small key/value bags whose exact keys are not
    documented, so unknown keys are printed as-is rather than dropped.
    """

    if not isinstance(mapping, dict):
        return esc(fmt.compact_json(mapping, 240))
    parts = []
    for key, value in list(mapping.items())[:14]:
        if isinstance(value, (dict, list)):
            value = fmt.compact_json(value, 60)
        label = _QUOTA_LABELS.get(str(key), str(key))
        parts.append(
            '<span class="chip">' + esc(label) + " " + esc(value) + "</span>"
        )
    if not parts:
        return '<span class="muted">(空)</span>'
    return '<div class="chips">' + "".join(parts) + "</div>"


def _value_html(value: Any) -> str:
    if isinstance(value, dict):
        return _chips(value)
    if isinstance(value, list):
        return _chips({str(index): item for index, item in enumerate(value[:14], 1)})
    if isinstance(value, bool):
        return "是" if value else "否"
    return esc(value)


def build_profile_html(data: Any, embed_images: bool = True) -> str:
    agent: Any = data
    envelope: Any = data if isinstance(data, dict) else {}
    if isinstance(data, dict) and isinstance(data.get("agent"), dict):
        agent = data["agent"]
    if not isinstance(agent, dict):
        agent = {}
    if envelope is not agent:
        # GET /me wraps the account in "agent" and hangs quota/stats next to it,
        # while GET /agents/{name} returns them inline. Flatten both shapes so
        # the view below only has to know one.
        merged = dict(envelope)
        merged.pop("agent", None)
        merged.update(agent)
        agent = merged
    name = str(fmt.pick(agent, "name", default="?") or "?")
    signature = fmt.pick(agent, "signature", default="")

    hero = (
        '<div class="card"><div class="hero">'
        + _avatar_html(name, fmt.pick(agent, "avatar_url", "avatar", default=""), embed_images)
        + '<div class="rowmain"><div class="who2">'
        + esc(name)
        + (
            _badge("已认领", "me")
            if fmt.pick(agent, "claimed", "is_claimed")
            else _badge("未认领")
        )
        + "</div>"
        + (
            '<div class="sig">'
            + _inline(esc(fmt.truncate(signature, 120)), False)
            + "</div>"
            if signature
            else ""
        )
        + "</div></div></div>"
    )

    rows = [
        _kv(
            "简介",
            _inline(esc(fmt.truncate(fmt.pick(agent, "bio", default=""), 400)), False)
            or '<span class="muted">(空)</span>',
        )
    ]
    for label, keys in (
        ("等级", ("level", "rank")),
        ("声望", ("reputation", "rep", "score")),
        ("公开额度", ("quota", "quotas", "public_quota", "write_quota")),
        ("框架", ("framework",)),
    ):
        value = fmt.pick(agent, *keys, default="")
        if value in (None, "", {}, []):
            continue
        rows.append(_kv(label, _value_html(value)))

    stats = agent.get("stats") if isinstance(agent.get("stats"), dict) else None
    if stats:
        rows.append(_kv("统计", _chips(stats)))

    owned = fmt.as_list(agent, "bars", "owned_bars")
    if owned:
        chips = "".join(
            '<span class="chip">'
            + esc(fmt.truncate(fmt.pick(item, "slug", "name", default="?"), 30))
            + "</span>"
            for item in owned[:12]
        )
        rows.append(_kv("拥有的吧", '<div class="chips">' + chips + "</div>"))

    bans = fmt.as_list(agent, "bans")
    if bans:
        rows.append(_kv("封禁记录", esc(fmt.compact_json(bans, 300))))

    body = hero + '<div class="card">' + "".join(rows) + "</div>"
    return _page("档案", esc(name), "", body, esc(SITE_ORIGIN + "/u/" + name))


# ----------------------------------------------------------------- search view


def build_search_html(data: Any, query: str = "", limit: int = DEFAULT_MAX_ITEMS) -> str:
    cap = max(1, min(int(limit or DEFAULT_MAX_ITEMS), HARD_MAX_ITEMS))
    threads = fmt.as_list(data, "threads")
    bars = fmt.as_list(data, "bars")
    agents = fmt.as_list(data, "agents")
    blocks: list[str] = []

    if threads:
        blocks.append('<div class="sect">主题 ' + str(len(threads)) + " 条</div>")
        for item in threads[:cap]:
            blocks.append(
                '<div class="card row"><div class="rowmain"><div class="rowtitle">'
                + esc(fmt.truncate(fmt.pick(item, "title", default="(无标题)"), 90))
                + '</div><div class="rowmeta">'
                + _dot(
                    [
                        '<span class="bar">'
                        + esc(fmt.pick(item, "bar", "bar_slug", default="?"))
                        + " 吧</span>",
                        esc(fmt.author_of(item)),
                        esc(_short_id(fmt.pick(item, "id", "_id", default=""))),
                    ]
                )
                + "</div></div></div>"
            )
    if bars:
        blocks.append('<div class="sect">吧 ' + str(len(bars)) + " 个</div>")
        for item in bars[:cap]:
            blocks.append(
                '<div class="card row"><div class="rowmain"><div class="rowtitle">'
                + esc(fmt.pick(item, "slug", default="?"))
                + " · "
                + esc(fmt.pick(item, "name", default=""))
                + '</div><div class="rowmeta">'
                + _dot(
                    [
                        esc(fmt.category_label(fmt.pick(item, "category"))),
                        "主题 " + esc(fmt.pick(item, "thread_count", "threads", default="?")),
                        "吧主 " + esc(fmt.pick(item, "owner_name", "owner", default="(空缺)")),
                    ]
                )
                + "</div></div></div>"
            )
    if agents:
        blocks.append('<div class="sect">AI ' + str(len(agents)) + " 个</div>")
        for item in agents[:cap]:
            blocks.append(
                '<div class="card row">'
                + _avatar_html(fmt.pick(item, "name", default="?"), "", False)
                + '<div class="rowmain"><div class="rowtitle">'
                + esc(fmt.pick(item, "name", default="?"))
                + '</div><div class="rowmeta">'
                + esc(fmt.truncate(fmt.pick(item, "bio", default=""), 100))
                + "</div></div></div>"
            )
    if not blocks:
        blocks.append('<div class="card"><p class="muted">没有命中结果。</p></div>')

    return _page(
        "搜索",
        esc(fmt.truncate(query, 60)) or "搜索结果",
        "",
        "".join(blocks),
        esc(SITE_ORIGIN),
    )


# ----------------------------------------------------------- notification view

_NOTIFICATION_LABELS: dict[str, str] = {
    "reply": "有人回你",
    "floor": "有人回你",
    "subfloor": "楼中楼回复",
    "mention": "被 @ 了",
    "at": "被 @ 了",
    "vote": "被顶踩",
    "upvote": "被顶",
    "downvote": "被踩",
    "message": "私信",
    "dm": "私信",
    "system": "系统",
    "bar": "吧务事件",
    "election": "选举",
}


def build_notifications_html(data: Any, limit: int = DEFAULT_MAX_ITEMS) -> str:
    items = fmt.as_list(data, "notifications", "items", "results")
    cap = max(1, min(int(limit or DEFAULT_MAX_ITEMS), HARD_MAX_ITEMS))
    rows: list[str] = []
    for item in items[:cap]:
        kind = str(fmt.pick(item, "type", "kind", default="") or "")
        label = _NOTIFICATION_LABELS.get(kind.lower(), kind or "通知")
        actor = str(fmt.pick(item, "from_name", "from", "actor", default="?") or "?")
        payload = item.get("payload") if isinstance(item, dict) else None
        excerpt = ""
        if isinstance(payload, dict):
            excerpt = fmt.truncate(
                fmt.pick(payload, "body", "text", "title", "excerpt", default=""), 200
            )
        rows.append(
            '<div class="card row">'
            + _avatar_html(actor, "", False)
            + '<div class="rowmain"><div class="rowtitle">'
            + _badge(label)
            + " "
            + esc(actor)
            + "</div>"
            + (
                '<div class="body"><p>' + _inline(esc(excerpt), False) + "</p></div>"
                if excerpt
                else ""
            )
            + '<div class="rowmeta">'
            + _dot(
                [
                    esc(fmt.rel_time(fmt.pick(item, "created_at"))),
                    esc(_short_id(fmt.pick(item, "id", "_id", default=""))),
                ]
            )
            + "</div></div></div>"
        )
    if not rows:
        rows.append('<div class="card"><p class="muted">没有未读通知。</p></div>')
    subtitle = "共 " + str(len(items)) + " 条" + (
        "，画了前 " + str(cap) + " 条" if len(items) > cap else ""
    )
    return _page("通知", "未读通知", subtitle, "".join(rows))


# --------------------------------------------------------------- target parsing

_ID_RE = re.compile(r"^[0-9a-f]{24}$")
_THREAD_URL_RE = re.compile(r"/t/([0-9a-f]{24})")
_BAR_URL_RE = re.compile(r"/b/([^/\s#?]+)")
_USER_URL_RE = re.compile(r"/u/([^/\s#?]+)")


def parse_target(text: Any) -> tuple[str, str]:
    """Guess a view and a target from whatever the owner pasted.

    The whole point of this tool is that a human can say "看看这个" plus a URL and
    get a picture back, so a URL, a bare id and a slug all have to work without
    the model classifying them first. An empty view means "caller decides".
    """

    raw = str(text or "").strip()
    if not raw:
        return "", ""
    match = _THREAD_URL_RE.search(raw)
    if match:
        return "thread", match.group(1)
    match = _BAR_URL_RE.search(raw)
    if match:
        return "feed", unquote(match.group(1))
    match = _USER_URL_RE.search(raw)
    if match:
        return "profile", unquote(match.group(1))
    if _ID_RE.match(raw.lower()):
        return "thread", raw.lower()
    return "", raw


# -------------------------------------------------------------------- renderer


@dataclass
class SnapshotResult:
    """Everything the caller needs in order to actually send the thing."""

    view: str = ""
    image_path: str = ""
    caption: str = ""
    text: str = ""
    engine: str = "none"
    note: str = ""

    @property
    def has_image(self) -> bool:
        return bool(self.image_path)


@dataclass
class SnapshotRenderer:
    """Adapter over AstrBot's two rendering paths, with a graceful ladder.

    html_render talks to a remote t2i service and can fail for reasons nobody
    here controls (empty endpoint list, network, Playwright rejecting an option),
    so a failure must not lose the content: it falls back to text_to_image, which
    itself falls back to local PIL inside AstrBot. If both fail we still return
    normally and the caller sends plain text. Both callables are injected, which
    is what makes the ladder testable without a network.
    """

    html_render: Callable[..., Awaitable[Any]] | None = None
    text_to_image: Callable[..., Awaitable[Any]] | None = None
    quality: int = 92
    enabled: bool = True

    async def render(self, page_html: str, text: str = "") -> tuple[str, str, str]:
        """Return (local_file_path, engine, note); path is empty if both failed."""

        if not self.enabled:
            # The service checks the same switch, but a renderer handed to some
            # other caller must not quietly ignore it.
            return "", "none", "截图功能被关掉了（snapshot_enabled = false）。"

        note = ""
        if self.html_render is not None:
            try:
                path = await self.html_render(
                    page_html,
                    {},
                    return_url=False,
                    options={
                        "full_page": True,
                        "type": "jpeg",
                        "quality": max(30, min(int(self.quality or 92), 100)),
                    },
                )
                if path:
                    return str(path), "html", ""
                note = "网页渲染没有返回文件。"
            except Exception as error:  # noqa: BLE001 - remote service, any failure
                note = (
                    "网页渲染失败（"
                    + type(error).__name__
                    + ": "
                    + str(error)[:160]
                    + "）。"
                )
        if self.text_to_image is not None and text:
            try:
                path = await self.text_to_image(text, return_url=False)
                if path:
                    return str(path), "t2i", note + "已降级成纯文字转图。"
            except Exception as error:  # noqa: BLE001
                note += "文字转图也失败（" + type(error).__name__ + "）。"
        return "", "none", note or "本机没有可用的渲染后端。"
