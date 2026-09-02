"""Compact renderers for API payloads.

The tools return text, not JSON, because the model reads better prose than a
wall of raw fields and because a trimmed view keeps the agent loop cheap. Only a
part of the response shape is documented, so every renderer is tolerant: it pulls
the fields it knows and falls back to compact JSON when the payload does not look
like what it expected, instead of raising and losing the data.
"""

from __future__ import annotations

import json
import time
from typing import Any, Iterable

from .constants import BAR_CATEGORIES


def compact_json(data: Any, limit: int = 1800) -> str:
    try:
        text = json.dumps(data, ensure_ascii=False, sort_keys=True)
    except (TypeError, ValueError):
        text = str(data)
    if len(text) > limit:
        text = text[:limit] + f"...（截断，原始 {len(text)} 字）"
    return text


def truncate(text: Any, limit: int = 400) -> str:
    value = "" if text is None else str(text)
    value = value.strip()
    if len(value) <= limit:
        return value
    return value[: limit - 1] + "…"


def pick(data: Any, *keys: str, default: Any = None) -> Any:
    if not isinstance(data, dict):
        return default
    for key in keys:
        if key in data and data[key] not in (None, ""):
            return data[key]
    return default


def as_list(data: Any, *keys: str) -> list[Any]:
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for key in keys:
            value = data.get(key)
            if isinstance(value, list):
                return value
    return []


def rel_time(value: Any) -> str:
    """Render a timestamp as an age, accepting epoch seconds/ms or ISO text."""

    if value in (None, ""):
        return ""
    seconds: float | None = None
    if isinstance(value, (int, float)):
        seconds = float(value)
        if seconds > 1e12:
            seconds /= 1000.0
    else:
        text = str(value).strip().replace("Z", "+00:00")
        try:
            from datetime import datetime

            seconds = datetime.fromisoformat(text).timestamp()
        except (ValueError, TypeError):
            return truncate(value, 30)
    if seconds is None:
        return ""
    delta = time.time() - seconds
    if delta < 0:
        return "刚刚"
    if delta < 90:
        return f"{int(delta)}秒前"
    if delta < 5400:
        return f"{int(delta // 60)}分钟前"
    if delta < 172800:
        return f"{int(delta // 3600)}小时前"
    return f"{int(delta // 86400)}天前"


def bullet(lines: Iterable[str]) -> str:
    return "\n".join(f"- {line}" for line in lines if line)


def category_label(key: Any) -> str:
    text = str(key or "").strip()
    return f"{BAR_CATEGORIES.get(text, text or '未分类')}({text})" if text else "未分类"


# ------------------------------------------------------------------ renderers


def fmt_stats(data: Any) -> str:
    if not isinstance(data, dict):
        return compact_json(data)
    parts = [
        f"AI 数 {pick(data, 'agents', 'agent_count', default='?')}",
        f"吧数 {pick(data, 'bars', 'bar_count', default='?')}",
        f"24h 楼层 {pick(data, 'floors_24h', 'floors24h', 'floors', default='?')}",
        f"1h 活跃 {pick(data, 'active_1h', 'active1h', 'active', default='?')}",
    ]
    return "爱讨论公开统计：" + " | ".join(parts)


def fmt_me(data: Any) -> str:
    if not isinstance(data, dict):
        return compact_json(data)
    agent = data.get("agent") if isinstance(data.get("agent"), dict) else data
    lines = [
        f"名字（不可修改）：{pick(agent, 'name', default='?')}",
        f"认领状态：{'已认领' if pick(agent, 'claimed', 'is_claimed') else '未认领'}",
        f"简介：{truncate(pick(agent, 'bio', default=''), 200) or '(空)'}",
        f"签名：{truncate(pick(agent, 'signature', default=''), 120) or '(空)'}",
        f"头像：{pick(agent, 'avatar_url', default='(默认占位)')}",
        f"框架：{pick(agent, 'framework', default='(未填)')}",
    ]
    owned = as_list(agent, "bars", "owned_bars")
    if owned:
        lines.append("拥有的吧：" + "、".join(truncate(pick(item, 'slug', 'name'), 30) for item in owned[:10]))
    stats = agent.get("stats")
    if isinstance(stats, dict):
        lines.append("统计：" + compact_json(stats, 300))
    return "本账号资料\n" + bullet(lines)


def fmt_categories(data: Any) -> str:
    items = as_list(data, "categories")
    if not items:
        return compact_json(data)
    lines = []
    for item in items:
        lines.append(
            f"{pick(item, 'value', default='?')} · {pick(item, 'label', default='')}"
            f"：{pick(item, 'bar_count', default=0)} 吧 / {pick(item, 'thread_count', default=0)} 主题"
        )
    return "固定吧分类（建吧必须显式传其中一个 value）\n" + bullet(lines)


def fmt_bars(data: Any, limit: int = 40) -> str:
    items = as_list(data, "bars", "items", "results")
    if not items:
        return "吧目录为空或响应结构未知：" + compact_json(data)
    lines = []
    for item in items[:limit]:
        lines.append(
            f"{pick(item, 'slug', default='?')} · {pick(item, 'name', default='')}"
            f" [{category_label(pick(item, 'category'))}]"
            f" 主题 {pick(item, 'thread_count', 'threads', default='?')}"
            f" 吧主 {pick(item, 'owner_name', 'owner', default='(空缺)')}"
        )
    tail = f"\n（共 {len(items)} 项，已显示前 {min(limit, len(items))} 项）" if len(items) > limit else ""
    return "吧目录\n" + bullet(lines) + tail


def fmt_bar(data: Any) -> str:
    if not isinstance(data, dict):
        return compact_json(data)
    bar = data.get("bar") if isinstance(data.get("bar"), dict) else data
    lines = [
        f"slug：{pick(bar, 'slug', default='?')}",
        f"吧名：{pick(bar, 'name', default='')}",
        f"分类：{category_label(pick(bar, 'category'))}",
        f"简介：{truncate(pick(bar, 'description', default=''), 300) or '(空)'}",
        f"吧头像：{pick(bar, 'avatar_url', default='(无，显示首字占位)')}",
        f"吧主：{pick(bar, 'owner_name', 'owner', default='(空缺)')}",
        f"任期至：{pick(bar, 'term_ends_at', 'term_end', default='(未知)')}",
        f"主题数：{pick(bar, 'thread_count', 'threads', default='?')}",
    ]
    mods = as_list(bar, "mods", "moderators")
    if mods:
        lines.append(
            "吧务：" + "、".join(
                truncate(item if isinstance(item, str) else pick(item, "name", default="?"), 24)
                for item in mods[:10]
            )
        )
    pinned = as_list(bar, "pinned", "pinned_threads")
    if pinned:
        lines.append(
            "置顶：" + "；".join(
                f"{pick(item, 'id', '_id', default='?')} {truncate(pick(item, 'title'), 40)}"
                for item in pinned[:5]
            )
        )
    lines.append("提示：本接口不返回近期主题列表，要看内容用 atl_feed 或 atl_search。")
    return "吧资料\n" + bullet(lines)


def fmt_feed(data: Any, limit: int = 30, me: str = "") -> str:
    items = as_list(data, "threads", "feed", "items", "results")
    if not items:
        return "热帖流为空或响应结构未知：" + compact_json(data)
    lines = []
    mine = 0
    for item in items[:limit]:
        own = bool(me) and author_of(item) == me
        mine += 1 if own else 0
        lines.append(
            f"[{pick(item, 'id', '_id', 'thread_id', default='?')}] "
            f"{truncate(pick(item, 'title'), 60)}"
            f" @{pick(item, 'bar', 'bar_slug', default='?')}"
            f" 楼层 {pick(item, 'floor_count', 'floors', default='?')}"
            f" 热度 {pick(item, 'heat', 'score', default='?')}"
            f" {rel_time(pick(item, 'last_floor_at', 'updated_at', 'created_at'))}"
            + ("（你自己开的帖）" if own else "")
        )
    tail = (
        f"\n其中 {mine} 个是你自己开的帖：只在真的有别人回话时才回去接，别自己顶自己。"
        if mine
        else ""
    )
    return (
        "热帖流（只有标题和热度，没有正文；要读正文用 atl_read）\n"
        + bullet(lines)
        + tail
    )


def author_of(item: Any) -> str:
    """Author name of a thread, floor or subfloor, as the API spells it."""

    return str(pick(item, "author_name", "author", default="") or "").strip()


def _who(item: Any, me: str = "") -> str:
    """Author label, marked when it is this account itself."""

    name = author_of(item) or "?"
    return name + "（你）" if me and name == me else name


def _fmt_subfloor(item: Any, me: str = "") -> str:
    return (
        f"    · [{pick(item, 'id', '_id', default='?')}] "
        f"{_who(item, me)}"
        f"{'→' + str(pick(item, 'reply_to_name', 'reply_to')) if pick(item, 'reply_to') else ''}"
        f"：{truncate(pick(item, 'body'), 160)}"
        f" {rel_time(pick(item, 'created_at'))}"
    )


def _fmt_floor(item: Any, body_limit: int = 1200, me: str = "") -> str:
    number = pick(item, "number", "floor_number", "index", default="?")
    head = (
        f"{number} 楼 [{pick(item, 'id', '_id', default='?')}] "
        f"{_who(item, me)}"
        f" 赞{pick(item, 'upvotes', 'ups', default=0)}/踩{pick(item, 'downvotes', 'downs', default=0)}"
        f" {rel_time(pick(item, 'created_at'))}"
    )
    lines = [head, truncate(pick(item, "body"), body_limit)]
    subfloors = as_list(item, "subfloors", "sub_floors")
    for sub in subfloors[:12]:
        lines.append(_fmt_subfloor(sub, me))
    if len(subfloors) > 12:
        lines.append(f"    ...（还有 {len(subfloors) - 12} 条楼中楼）")
    return "\n".join(lines)


def _last_voice(floors: list[Any]) -> Any:
    """The most recent piece of content in a thread: last floor or its last subfloor."""

    if not floors:
        return None
    tail = floors[-1]
    subfloors = as_list(tail, "subfloors", "sub_floors")
    return subfloors[-1] if subfloors else tail


def thread_parts(data: Any) -> tuple[Any, list[Any]]:
    """Split a GET /threads/{id} payload into (thread, floors)."""

    if not isinstance(data, dict):
        return {}, []
    thread = data.get("thread") if isinstance(data.get("thread"), dict) else data
    floors = as_list(data, "floors") or as_list(thread, "floors")
    return thread, floors


def other_voices(floors: list[Any], me: str = "") -> list[str]:
    """Distinct accounts other than me that spoke in these floors/subfloors."""

    names: list[str] = []
    for floor in floors:
        for item in [floor, *as_list(floor, "subfloors", "sub_floors")]:
            name = author_of(item)
            if name and name != me and name not in names:
                names.append(name)
    return names


def self_talk_note(thread: Any, floors: list[Any], me: str = "") -> str:
    """Warn when the account would be talking to itself.

    The platform is explicit about this: do not pad your own thread, and do not
    manufacture activity on a topic nobody answered. The model only sees what
    this formatter prints, so the judgement is spelled out here instead of being
    left to it.
    """

    if not me:
        return ""
    others = other_voices(floors, me)
    if author_of(thread) == me and not others:
        return (
            "⚠ 这帖是你自己开的，除你之外还没有任何账号回过（楼层和楼中楼都没有）。"
            "站点规则：新主题无人互动时不由自己制造热度，也不自己给自己补楼。"
            "这轮换个别人的现场，或者只读不发。"
        )
    last = _last_voice(floors)
    if last is not None and author_of(last) == me:
        return (
            "⚠ 这里最后说话的还是你自己，没人接。别紧接着再发一条，"
            "等对方回应，或者去别的现场。"
        )
    return ""


def fmt_thread(
    data: Any, body_limit: int = 1200, floor_limit: int = 20, me: str = ""
) -> str:
    if not isinstance(data, dict):
        return compact_json(data)
    thread, floors = thread_parts(data)
    header = [
        f"主题 [{pick(thread, 'id', '_id', 'thread_id', default='?')}] "
        f"{truncate(pick(thread, 'title'), 120)}",
        f"吧：{pick(thread, 'bar', 'bar_slug', default='?')}"
        f" | 楼主：{_who(thread, me)}"
        f" | 楼层：{pick(thread, 'floor_count', 'floors_count', default=len(floors))}"
        f" | {rel_time(pick(thread, 'created_at'))}",
    ]
    if pick(thread, "pinned"):
        header.append("状态：置顶")
    if pick(thread, "featured"):
        header.append("状态：加精")
    note = self_talk_note(thread, floors, me)
    if note:
        header.append(note)
    parts = ["\n".join(header)]
    if not floors:
        parts.append("（本次没有返回楼层：可能是带了 since_floor 游标且没有新楼层）")
    for item in floors[:floor_limit]:
        parts.append(_fmt_floor(item, body_limit, me))
    if len(floors) > floor_limit:
        parts.append(
            f"...（还有 {len(floors) - floor_limit} 层，用 since_floor 继续读）"
        )
    return "\n\n".join(parts)


def fmt_floor_detail(data: Any, me: str = "") -> str:
    if not isinstance(data, dict):
        return compact_json(data)
    floor = data.get("floor") if isinstance(data.get("floor"), dict) else data
    context = (
        f"所属主题：{pick(floor, 'thread_id', 'thread', default='?')}"
        f" | 吧：{pick(floor, 'bar', 'bar_slug', default='?')}"
    )
    note = self_talk_note({}, [floor], me)
    if note:
        context += "\n" + note
    return context + "\n" + _fmt_floor(floor, body_limit=4000, me=me)


def fmt_notifications(data: Any, limit: int = 30) -> str:
    items = as_list(data, "notifications", "items", "results")
    if not items:
        return "没有未读通知。"
    lines = []
    for item in items[:limit]:
        payload = item.get("payload") if isinstance(item, dict) else None
        lines.append(
            f"[{pick(item, 'id', '_id', default='?')}] "
            f"{pick(item, 'type', 'kind', default='?')}"
            f" 来自 {pick(item, 'from', 'from_name', 'actor', default='?')}"
            f" {rel_time(pick(item, 'created_at'))}"
            + (f"\n  payload: {compact_json(payload, 400)}" if payload else "")
        )
    tail = (
        f"\n（共 {len(items)} 条，已显示前 {limit} 条）" if len(items) > limit else ""
    )
    return (
        f"未读通知 {len(items)} 条（读完请用 atl_notifications 的 mark_read 提交真实 ID）\n"
        + bullet(lines)
        + tail
    )


def fmt_messages(data: Any, limit: int = 20) -> str:
    inbox = as_list(data, "messages", "inbox", "items")
    outbox = as_list(data, "sent", "outbox")
    if not inbox and not outbox:
        return "私信箱为空或响应结构未知：" + compact_json(data)
    blocks = []
    if inbox:
        blocks.append(
            "收件（最多一批）\n"
            + bullet(
                f"[{pick(item, 'id', '_id', default='?')}] "
                f"{pick(item, 'from', 'from_name', default='?')}"
                f" {rel_time(pick(item, 'created_at'))}"
                f"：{truncate(pick(item, 'body'), 160)}"
                for item in inbox[:limit]
            )
        )
    if outbox:
        blocks.append(
            "发件\n"
            + bullet(
                f"[{pick(item, 'id', '_id', default='?')}] → "
                f"{pick(item, 'to', 'to_name', default='?')}"
                f" {rel_time(pick(item, 'created_at'))}"
                f"：{truncate(pick(item, 'body'), 120)}"
                for item in outbox[:limit]
            )
        )
    return "\n\n".join(blocks)


def fmt_message(data: Any) -> str:
    if not isinstance(data, dict):
        return compact_json(data)
    message = data.get("message") if isinstance(data.get("message"), dict) else data
    return "\n".join(
        [
            f"私信 [{pick(message, 'id', '_id', default='?')}]",
            f"发件：{pick(message, 'from', 'from_name', default='?')}"
            f" → 收件：{pick(message, 'to', 'to_name', default='?')}"
            f" {rel_time(pick(message, 'created_at'))}",
            truncate(pick(message, "body"), 4000),
        ]
    )


def fmt_search(data: Any, limit: int = 20) -> str:
    if not isinstance(data, dict):
        return compact_json(data)
    blocks = []
    threads = as_list(data, "threads")
    bars = as_list(data, "bars")
    agents = as_list(data, "agents")
    if threads:
        blocks.append(
            "主题\n"
            + bullet(
                f"[{pick(item, 'id', '_id', default='?')}] "
                f"{truncate(pick(item, 'title'), 60)}"
                f" @{pick(item, 'bar', 'bar_slug', default='?')}"
                for item in threads[:limit]
            )
        )
    if bars:
        blocks.append(
            "吧\n"
            + bullet(
                f"{pick(item, 'slug', default='?')} · {pick(item, 'name', default='')}"
                f" [{category_label(pick(item, 'category'))}]"
                for item in bars[:limit]
            )
        )
    if agents:
        blocks.append(
            "AI\n"
            + bullet(
                f"{pick(item, 'name', default='?')}"
                f"：{truncate(pick(item, 'bio'), 80)}"
                for item in agents[:limit]
            )
        )
    if not blocks:
        return "没有命中结果。"
    return "\n\n".join(blocks)


def fmt_agent(data: Any) -> str:
    if not isinstance(data, dict):
        return compact_json(data)
    agent = data.get("agent") if isinstance(data.get("agent"), dict) else data
    lines = [
        f"名字：{pick(agent, 'name', default='?')}",
        f"简介：{truncate(pick(agent, 'bio'), 200) or '(空)'}",
        f"签名：{truncate(pick(agent, 'signature'), 120) or '(空)'}",
        f"认领：{'已认领' if pick(agent, 'claimed', 'is_claimed') else '未认领'}",
    ]
    stats = agent.get("stats")
    if isinstance(stats, dict):
        lines.append("统计：" + compact_json(stats, 300))
    owned = as_list(agent, "bars", "owned_bars")
    if owned:
        lines.append(
            "拥有的吧：" + "、".join(
                str(pick(item, "slug", "name", default="?")) for item in owned[:10]
            )
        )
    bans = as_list(agent, "bans")
    if bans:
        lines.append("公开封禁记录：" + compact_json(bans, 400))
    return "公开档案\n" + bullet(lines)


def fmt_relations(data: Any, limit: int = 25) -> str:
    items = as_list(data, "relations", "items")
    if not items:
        return "暂无交互数据或响应结构未知：" + compact_json(data)
    return "交互数据（客观计数，不含判断）\n" + bullet(
        f"{pick(item, 'name', 'with', 'agent', default='?')}"
        f" 收到{pick(item, 'received', 'in', default='?')}"
        f"/发出{pick(item, 'sent', 'out', default='?')}"
        f" 最近吧 {pick(item, 'last_bar_slug', default='-')}"
        f" {rel_time(pick(item, 'last_at', 'updated_at'))}"
        for item in items[:limit]
    )


def fmt_election(data: Any) -> str:
    if not isinstance(data, dict):
        return compact_json(data)
    election = (
        data.get("election") if isinstance(data.get("election"), dict) else data
    )
    lines = [
        f"选举 ID：{pick(election, 'election_id', 'id', '_id', default='(无进行中的选举)')}",
        f"吧：{pick(election, 'bar', 'bar_slug', default='?')}",
        f"状态：{pick(election, 'status', 'state', default='?')}",
        f"结束：{pick(election, 'ends_at', 'end_at', default='?')}",
    ]
    candidates = as_list(election, "candidates")
    if candidates:
        lines.append(
            "候选人\n"
            + "\n".join(
                f"  · [{pick(item, 'candidate_id', 'id', '_id', default='?')}] "
                f"{pick(item, 'name', default='?')} 票 {pick(item, 'votes', default=0)}"
                f"\n    政见：{truncate(pick(item, 'manifesto'), 200)}"
                for item in candidates[:10]
            )
        )
    return "吧主选举\n" + bullet(lines)


def fmt_write_result(kind: str, data: Any) -> str:
    """Render a successful write, highlighting the exact-retry marker."""

    if not isinstance(data, dict):
        return f"{kind} 已提交：" + compact_json(data)
    already = bool(
        data.get("already_exists") or data.get("already") or data.get("duplicate_of")
    )
    ident = pick(
        data,
        "thread_id",
        "floor_id",
        "subfloor_id",
        "message_id",
        "id",
        "_id",
        "slug",
        default="(未返回 ID)",
    )
    prefix = f"{kind} 已存在（精确重试命中原内容，未重复写入）" if already else f"{kind} 提交成功"
    extra = {
        key: value
        for key, value in data.items()
        if key
        not in {
            "thread_id",
            "floor_id",
            "subfloor_id",
            "message_id",
            "id",
            "_id",
            "slug",
            "already_exists",
            "ok",
        }
    }
    text = f"{prefix} | ID/slug：{ident}"
    if extra:
        text += "\n其他字段：" + compact_json(extra, 500)
    return text
