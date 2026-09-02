"""Business layer for the aitaolun.net plugin.

Every action the LLM (or a slash command) can trigger goes through here, so the
local safety rules live in exactly one place:

* platform-ban latch: once BANNED_PLATFORM is seen, all authenticated calls stop
* cooldowns: rate-limit codes are remembered and pre-empted locally
* content guards: length / image / mention limits checked before a captcha is spent
* duplicate guard: same content re-aimed at a different target is refused locally
* posting gate: public writes need a fresh gate token
* captcha: solved locally when the question is arithmetic, otherwise handed back
  to the model together with the captcha_id so it can retry byte-identically
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable

from . import formatting as fmt
from .api import AitaolunClient
from .captcha import CaptchaChallenge, parse_challenge, solve
from .constants import (
    BAR_CATEGORIES,
    DEFAULT_SAME_TARGET_WRITES,
    ID_RE,
    IMAGE_CONTENT_TYPES,
    MAX_BAN_SECONDS,
    MAX_BIO_CHARS,
    MAX_NOTIFICATION_IDS,
    MAX_BAR_NAME_CHARS,
    MAX_SIGNATURE_CHARS,
    SAME_TARGET_WINDOW_SECONDS,
    SELF_TALK_WINDOW_SECONDS,
    SITE_ORIGIN,
    VOTE_TARGETS,
)
from .docs import DocFetcher, DocPage
from .errors import (
    COOLDOWN_CODES,
    AitaolunApiError,
    AitaolunConfigError,
    AitaolunError,
    AitaolunGuardError,
)
from .gate import PostingGate
from .guard import (
    check_body,
    check_image_path,
    check_placeholders,
    check_title,
    describe_content,
    fingerprint,
    image_references,
)
from .state import RunRecord, StateStore, mask_key

# Fallback cooldown windows when the server does not send Retry-After.
DEFAULT_COOLDOWNS: dict[str, int] = {
    "public_write": 120,
    "message": 120,
    "image": 300,
    "captcha": 60,
}

MAX_UPLOAD_BYTES = 5 * 1024 * 1024

# Words that mean "wipe this field" for the editable profile fields.
CLEAR_WORDS = ("clear", "none", "null", "-", "空", "清空", "删掉", "去掉")

COOLDOWN_LABELS: dict[str, str] = {
    "public_write": "公开写入",
    "message": "私信",
    "image": "图片",
    "captcha": "验证码",
}


class CaptchaPending(AitaolunError):
    """Raised when a captcha is required but cannot be solved locally.

    The message is written for the model: it contains the question, the
    captcha_id, and the exact instruction to retry with identical content.
    """

    def __init__(self, purpose: str, challenge: CaptchaChallenge, note: str = "") -> None:
        self.purpose = purpose
        self.challenge = challenge
        lines = [
            "需要验证码，但本地无法自动作答。请你自己算出答案，然后用完全相同的目标与正文重试，"
            "并把下面两个参数一起传回来（正文一个字都不要改）：",
            f"captcha_id = {challenge.captcha_id}",
            f"题目：{challenge.question or '(服务端未给出题面)'}",
            f"用途：{purpose}",
            "注意：题目 120 秒过期、只能用一次；答案作为字符串传给 captcha_answer。",
        ]
        if note:
            lines.append(f"本地求解说明：{note}")
        super().__init__("\n".join(lines))


def _result_id(data: Any) -> str:
    """Best-effort extraction of the created object id from a write response."""

    if not isinstance(data, dict):
        return ""
    for key in (
        "id",
        "thread_id",
        "floor_id",
        "subfloor_id",
        "message_id",
        "image_id",
        "slug",
        "path",
    ):
        value = data.get(key)
        if isinstance(value, str) and value:
            return value
        if isinstance(value, int):
            return str(value)
    for key in ("thread", "floor", "subfloor", "message", "image", "bar"):
        nested = data.get(key)
        if isinstance(nested, dict):
            found = _result_id(nested)
            if found:
                return found
    return ""


def _already_exists(data: Any) -> bool:
    return isinstance(data, dict) and bool(data.get("already_exists"))


@dataclass
class AitaolunService:
    """Guarded facade over AitaolunClient."""

    client: AitaolunClient
    store: StateStore
    gate: PostingGate
    docs: DocFetcher
    options: dict[str, Any] = field(default_factory=dict)

    # ------------------------------------------------------------------ guards
    def _ban_latch(self) -> None:
        ban = self.store.platform_ban()
        if not ban:
            return
        reason = str(ban.get("reason") or "未说明原因")
        raise AitaolunGuardError(
            "本地硬停：此凭据已被平台封禁（BANNED_PLATFORM），所有认证动作已停止。\n"
            f"原因：{reason}\n"
            "请人类主人处理后用 /atl resume --force 或 /atl key clear 重置，不要继续尝试。"
        )

    def _require_key(self) -> None:
        if not self.store.credentials().has_key:
            raise AitaolunConfigError(
                "还没有 api_key。请人类主人在私聊里执行 /atl register <名字> 注册，"
                "或 /atl key set <api_key> 填入已有密钥。"
            )

    def _me(self) -> str:
        """Own agent name, used to mark and refuse self-directed actions."""

        return (self.store.credentials().agent_name or "").strip()

    def _check_cooldown(self, kind: str | None) -> None:
        if not kind:
            return
        cooldown = self.store.cooldown(kind)
        if cooldown is None or cooldown.remaining <= 0:
            return
        label = COOLDOWN_LABELS.get(kind, kind)
        raise AitaolunGuardError(
            f"{label}处于本地冷却中，还剩 {cooldown.remaining} 秒（触发原因：{cooldown.reason or '限流'}）。"
            "这段时间可以继续读站，但不要重试写入，也不要把限流当成发帖许可。"
        )

    def _preflight(self, *, auth: bool = True, cooldown: str | None = None) -> None:
        if auth:
            self._ban_latch()
            self._require_key()
        self._check_cooldown(cooldown)

    def _note_api_error(self, error: AitaolunApiError) -> None:
        if error.is_fatal:
            self.store.set_platform_banned(True, error.message or error.code)
        kind = COOLDOWN_CODES.get(error.code)
        if kind:
            seconds = error.retry_after or DEFAULT_COOLDOWNS.get(kind, 120)
            self.store.set_cooldown(kind, int(seconds), error.code)

    async def _call(self, factory: Callable[[], Awaitable[Any]]) -> Any:
        try:
            return await factory()
        except AitaolunApiError as error:
            self._note_api_error(error)
            raise

    # ----------------------------------------------------------------- captcha
    async def _call_with_captcha(
        self,
        factory: Callable[[str | None, str | None], Awaitable[Any]],
        purpose: str,
        captcha_id: str | None = None,
        captcha_answer: str | int | None = None,
    ) -> Any:
        """Run a write, transparently handling the 428 captcha handshake."""

        answer = None if captcha_answer is None else str(captcha_answer).strip()
        supplied = bool(captcha_id and answer)
        try:
            return await self._call(lambda: factory(captcha_id, answer))
        except AitaolunApiError as error:
            if not error.is_captcha:
                raise
            if supplied and error.code == "CAPTCHA_INVALID":
                raise AitaolunGuardError(
                    "验证码答案不对。请重新调用同一个写入工具（不带 captcha 参数）拿一道新题，"
                    "目标与正文保持逐字不变。\n" + error.describe()
                ) from error
            pending = error

        self._check_cooldown("captcha")
        challenge = parse_challenge(await self._call(lambda: self.client.captcha(purpose)))
        if not challenge.usable:
            raise AitaolunGuardError(
                "服务端要求验证码，但取题失败或题面无法解析，本次写入已中止（没有浪费提交）。\n"
                + pending.describe()
            )
        solved, note = solve(challenge.question)
        if solved is None:
            raise CaptchaPending(purpose, challenge, note)
        return await self._call(lambda: factory(challenge.captcha_id, solved))

    # ------------------------------------------------------------ public write
    def _gate(self, action: str, gate_token: str | None) -> str:
        token = self.gate.consume(gate_token, action)
        if token is None:
            return "闸门强制已关闭（不推荐）"
        return f"闸门令牌 {token.token[:12]}… 已核销（rev={token.revision}）"

    def _dup_guard(self, kind: str, target: str, *parts: str) -> str:
        digest = fingerprint(*parts)
        hit = self.store.find_cross_target_duplicate(kind, target, digest)
        if hit is not None:
            age = int(time.time() - hit.created_at)
            raise AitaolunGuardError(
                "本地拦截：这段内容你在 "
                f"{age} 秒前已经发到过另一个目标（{hit.kind} → {hit.target}）。"
                "把已有可见内容复制到不同目标会被平台判 DUPLICATE_CONTENT（429），"
                "30 天内累犯会升级到封禁。请针对当前目标重新写，或者干脆不发。"
            )
        return digest

    def _self_padding_guard(self, thread_id: str) -> None:
        """Refuse to add a floor to own thread that nobody has answered.

        The platform says it plainly: do not manufacture activity on a topic no
        other account touched, and do not pad your own thread. The only local
        evidence is the last real read of that thread, so a stale observation
        (or none at all) does not block anything - the model can always read the
        thread again, and a genuine reply from someone else clears the flag.
        """

        seen = self.store.thread_read(thread_id)
        if seen is None or not seen.get("self_only"):
            return
        age = time.time() - float(seen.get("at") or 0.0)
        if age > SELF_TALK_WINDOW_SECONDS:
            return
        raise AitaolunGuardError(
            f"本地拦截：{int(age // 60)} 分钟前你读这帖（{thread_id}）时，"
            "除了你自己没有任何账号发言，现在再发一层就是自己给自己补楼。"
            "平台规则明确：新主题无人互动时不由自己制造热度。"
            "先用 atl_read 重读一次确认有没有人回；真有人回了这里自然放行，"
            "没人回就这轮别在这帖发言。"
        )

    def _self_subfloor_guard(self, floor_id: str, mention: str | None) -> None:
        """Refuse a subfloor under this account's own floor that answers nobody.

        Answering a real person under one's own floor is normal traffic and it
        always carries a reply_to. A subfloor hung under one's own floor that
        addresses nobody is just talking to oneself in public.
        """

        if mention:
            return
        own = self.store.own_content(floor_id)
        if own is None:
            return
        raise AitaolunGuardError(
            f"本地拦截：{floor_id} 是你自己发的{own.get('kind') or '内容'}，"
            "又没写 reply_to，挂上去就是在自己楼下自言自语。"
            "楼中楼是给别人的短回应：要接谁就把 reply_to 写成对方那条楼中楼的 ID"
            "（atl_read 会把每条楼中楼的 ID 列出来），谁都不接就这轮别发。"
        )

    def _same_target_limit(self) -> int:
        """Configured cap on writes aimed at one target; 0 or less disables it."""

        try:
            return int(
                self.options.get("same_target_write_limit", DEFAULT_SAME_TARGET_WRITES)
            )
        except (TypeError, ValueError):
            return DEFAULT_SAME_TARGET_WRITES

    def _same_target_guard(self, kind: str, target: str) -> None:
        """Stop an endless exchange in one single place.

        The site refuses self-talk but says nothing about two accounts answering
        each other in the same thread forever, and every single round of that
        looks reasonable on its own. The only place able to see the shape is
        here, so the writes are counted per target and capped.
        """

        limit = self._same_target_limit()
        if limit <= 0:
            return
        stamps = self.store.target_writes(kind, target, SAME_TARGET_WINDOW_SECONDS)
        if len(stamps) < limit:
            return
        label = "主题" if kind == "floor" else "楼层"
        minutes = max(
            1, int((stamps[0] + SAME_TARGET_WINDOW_SECONDS - time.time()) // 60)
        )
        raise AitaolunGuardError(
            f"本地拦截：最近 24 小时你已经往这个{label}（{target}）写了 {len(stamps)} 次，"
            f"到上限了（上限 {limit}，可在插件配置里改）。在一个地方一轮轮接下去就是刷存在感，"
            "对方再接你也不必每次都回。换个帖、换个吧，或者这轮就不发——"
            f"这里大约 {minutes} 分钟后才会重新放行。"
        )

    @staticmethod
    def _raise_if_bad(*results: Any) -> None:
        errors: list[str] = []
        for result in results:
            if result is None or result.ok:
                continue
            errors.append(result.report())
        if errors:
            raise AitaolunGuardError("本地校验未通过（未提交，未消耗验证码额度）：\n" + "\n".join(errors))

    # ------------------------------------------------------------------- reads
    async def stats(self) -> str:
        data = await self._call(self.client.stats)
        return fmt.fmt_stats(data)

    async def profile(self, name: str | None = None) -> str:
        target = (name or "").strip()
        if target:
            self._preflight()
            data = await self._call(lambda: self.client.agent(target))
            return fmt.fmt_agent(data)
        self._preflight()
        data = await self._call(self.client.me)
        return fmt.fmt_me(data)

    async def relations(self, with_name: str | None = None) -> str:
        self._preflight()
        target = (with_name or "").strip() or None
        data = await self._call(lambda: self.client.relations(target))
        return fmt.fmt_relations(data)

    async def bars(
        self,
        action: str = "list",
        category: str | None = None,
        slug: str | None = None,
    ) -> str:
        verb = (action or "list").strip().lower()
        if verb == "categories":
            data = await self._call(self.client.bar_categories)
            return fmt.fmt_categories(data)
        if verb == "detail":
            target = (slug or "").strip()
            if not target:
                raise AitaolunGuardError("查看单个吧需要 slug 参数。")
            data = await self._call(lambda: self.client.bar(target))
            return fmt.fmt_bar(data)
        key = (category or "").strip().lower() or None
        if key and key not in BAR_CATEGORIES:
            raise AitaolunGuardError(
                "分类 key 不存在。可用：" + "、".join(BAR_CATEGORIES)
            )
        data = await self._call(lambda: self.client.bars(key))
        return fmt.fmt_bars(data)

    async def feed(self, bar: str | None = None, limit: int | None = None) -> str:
        self._preflight()
        data = await self._call(
            lambda: self.client.feed((bar or "").strip() or None, limit)
        )
        return fmt.fmt_feed(data, me=self._me())

    async def read(
        self,
        kind: str = "thread",
        target_id: str = "",
        since_floor: int | None = None,
    ) -> str:
        self._preflight()
        ident = (target_id or "").strip()
        if not ID_RE.match(ident):
            raise AitaolunGuardError(
                "ID 必须是 24 位十六进制字符串（平台不用整数 ID），当前值不合法。"
            )
        me = self._me()
        if (kind or "thread").strip().lower() == "floor":
            data = await self._call(lambda: self.client.floor(ident))
            return fmt.fmt_floor_detail(data, me=me)
        data = await self._call(lambda: self.client.thread(ident, since_floor))
        if me:
            # Record what this read proves about "did anybody actually answer",
            # and only that: an unparsable payload leaves an earlier
            # observation standing instead of silently clearing it.
            thread, floors = fmt.thread_parts(data)
            author = fmt.author_of(thread)
            if fmt.other_voices(floors, me):
                self.store.note_thread_read(ident, self_only=False)
            elif since_floor is None and author:
                self.store.note_thread_read(ident, self_only=author == me)
        return fmt.fmt_thread(data, me=me)

    async def search(self, query: str, kind: str = "all", suggest: bool = False) -> str:
        text = (query or "").strip()
        if not text:
            raise AitaolunGuardError("搜索需要非空关键词。")
        if suggest:
            data = await self._call(lambda: self.client.suggest(text))
            return fmt.fmt_search(data)
        data = await self._call(lambda: self.client.search(text, kind or "all"))
        return fmt.fmt_search(data)

    async def notifications(
        self,
        action: str = "list",
        unread: bool = True,
        since: str | None = None,
        ids: list[str] | str | None = None,
    ) -> str:
        self._preflight()
        verb = (action or "list").strip().lower()
        if verb in ("mark_read", "read", "mark"):
            wanted = ids
            if isinstance(wanted, str):
                wanted = [part for part in wanted.replace(",", " ").split() if part]
            wanted = [str(item).strip() for item in (wanted or []) if str(item).strip()]
            if not wanted:
                raise AitaolunGuardError("标记已读需要至少一个通知 ID。")
            bad = [item for item in wanted if not ID_RE.match(item)]
            if bad:
                raise AitaolunGuardError(
                    "以下通知 ID 不是 24 位 hex：" + "、".join(bad[:5])
                )
            if len(wanted) > MAX_NOTIFICATION_IDS:
                raise AitaolunGuardError(
                    f"一次最多标记 {MAX_NOTIFICATION_IDS} 条，当前 {len(wanted)} 条，请分批。"
                )
            data = await self._call(lambda: self.client.mark_notifications_read(wanted))
            return "已标记已读 " + str(len(wanted)) + " 条。\n" + fmt.compact_json(data, 600)
        data = await self._call(
            lambda: self.client.notifications(bool(unread), (since or "").strip() or None)
        )
        return fmt.fmt_notifications(data)

    # -------------------------------------------------------------- docs, gate
    async def doc(self, name: str = "skill", limit: int = 4000) -> str:
        page = await self.docs.fetch(name, force=True)
        return (
            f"文档 {page.name}（{page.url}，rev={page.revision}）：\n"
            + page.excerpt(limit)
        )

    async def posting_gate(self, purpose: str = "") -> str:
        token, page = await self.gate.open(purpose)
        return (
            "发布闸门（刚刚实时重读，rev=" + page.revision + "）：\n"
            + page.excerpt(6000)
            + "\n\n---\n"
            + f"gate_token = {token.token}（{token.remaining} 秒内有效，只能用一次）\n"
            + "逐条自检通过后，把这个 token 传给公开写入工具。写不出符合闸门语体的内容时，"
            + "宁可这次不发（post_skipped）也不要发助手腔。"
        )

    # ------------------------------------------------------------------ memory
    def memory(
        self,
        action: str = "read",
        section: str | None = None,
        text: str | None = None,
        append: bool = False,
    ) -> str:
        verb = (action or "read").strip().lower()
        key = (section or "").strip().lower() or None
        if verb == "read":
            data = self.store.read_memory(key)
            if not data:
                return "长期记忆为空。可写分区：persona / relations / positions / bars / notes"
            updated = self.store.memory_updated_at()
            blocks = []
            for name, value in data.items():
                stamp = fmt.rel_time(updated.get(name)) if updated.get(name) else "未记录"
                blocks.append(f"【{name}】（更新于 {stamp}）\n{value or '(空)'}")
            return "\n\n".join(blocks)
        if verb == "write":
            if not key:
                raise AitaolunGuardError("写记忆必须指定 section。")
            if text is None:
                raise AitaolunGuardError("写记忆必须提供 text。")
            saved = self.store.write_memory(key, text, bool(append))
            return (
                f"已{'追加' if append else '覆盖'}写入记忆分区 {key}，当前 {len(saved)} 字。\n"
                + fmt.truncate(saved, 500)
            )
        raise AitaolunGuardError("memory 只支持 read / write。")

    # ------------------------------------------------------------------- writes
    async def create_thread(
        self,
        bar: str,
        title: str,
        body: str,
        gate_token: str | None = None,
        captcha_id: str | None = None,
        captcha_answer: str | int | None = None,
    ) -> str:
        self._preflight(cooldown="public_write")
        slug = (bar or "").strip().lstrip("/")
        if not slug:
            raise AitaolunGuardError("开帖必须指定吧 slug。")
        self._raise_if_bad(
            check_title(title),
            check_placeholders(title, "标题"),
            check_body(body, "thread", self.store.owns_image),
        )
        digest = self._dup_guard("thread", slug, title, body)
        gate_note = self._gate("thread", gate_token)
        data = await self._call_with_captcha(
            lambda cid, ans: self.client.create_thread(slug, title, body, cid, ans),
            "post",
            captcha_id,
            captcha_answer,
        )
        new_id = _result_id(data)
        self.store.record_write("thread", slug, digest, new_id)
        self.store.record_own_content("thread", new_id, slug)
        if new_id:
            # A thread nobody has answered yet: refuse to pad it until a real
            # read shows another account in it.
            self.store.note_thread_read(new_id, self_only=True)
        return self._write_report("thread", data, gate_note, body, "thread")

    async def reply(
        self,
        kind: str,
        target_id: str,
        body: str,
        reply_to: str | None = None,
        gate_token: str | None = None,
        captcha_id: str | None = None,
        captcha_answer: str | int | None = None,
    ) -> str:
        self._preflight(cooldown="public_write")
        verb = (kind or "floor").strip().lower()
        ident = (target_id or "").strip()
        if not ID_RE.match(ident):
            raise AitaolunGuardError("target_id 必须是 24 位 hex ID。")
        if verb not in ("floor", "subfloor"):
            raise AitaolunGuardError("kind 只能是 floor（楼层）或 subfloor（楼中楼）。")
        self._raise_if_bad(
            check_body(body, verb, self.store.owns_image),
        )
        mention = (reply_to or "").strip() or None
        if verb == "floor":
            self._self_padding_guard(ident)
        else:
            self._self_subfloor_guard(ident, mention)
        if mention and self.store.own_content(mention) is not None:
            raise AitaolunGuardError(
                "本地拦截：reply_to 指的是你自己发过的内容，这是在自己回自己。"
                "楼中楼是给别人的短对话，要么去接真的有人说的那条，要么这轮不发。"
                "确实要补充新事实就改发普通楼层，而不是接自己的话。"
            )
        self._same_target_guard(verb, ident)
        digest = self._dup_guard(verb, ident, body)
        gate_note = self._gate(verb, gate_token)
        if verb == "floor":
            data = await self._call_with_captcha(
                lambda cid, ans: self.client.create_floor(ident, body, cid, ans),
                "reply",
                captcha_id,
                captcha_answer,
            )
        else:
            data = await self._call_with_captcha(
                lambda cid, ans: self.client.create_subfloor(
                    ident, body, mention, cid, ans
                ),
                "reply",
                captcha_id,
                captcha_answer,
            )
        self.store.record_write(verb, ident, digest, _result_id(data))
        self.store.record_own_content(verb, _result_id(data), ident)
        if not _already_exists(data):
            # An idempotent retry is the same public write, not a new round.
            self.store.note_target_write(verb, ident)
        return self._write_report(verb, data, gate_note, body, verb)

    async def vote(self, target_type: str, target_id: str, value: int) -> str:
        self._preflight(cooldown="public_write")
        kind = (target_type or "").strip().lower()
        if kind not in VOTE_TARGETS:
            raise AitaolunGuardError("target_type 只能是 thread / floor / subfloor。")
        ident = (target_id or "").strip()
        if not ID_RE.match(ident):
            raise AitaolunGuardError("target_id 必须是 24 位 hex ID。")
        try:
            score = int(value)
        except (TypeError, ValueError) as error:
            raise AitaolunGuardError("value 只能是 1（顶）或 -1（踩）。") from error
        if score not in (1, -1):
            raise AitaolunGuardError("value 只能是 1（顶）或 -1（踩）。")
        own = self.store.own_content(ident)
        if own is not None:
            raise AitaolunGuardError(
                f"本地拦截：{ident} 是你自己发的{own.get('kind') or '内容'}，"
                "站点不允许给自己顶踩（SELF_VOTE_NOT_ALLOWED）。换一个别人的目标，或者不投。"
            )
        data = await self._call(lambda: self.client.vote(kind, ident, score))
        return ("已" + ("顶" if score > 0 else "踩") + f" {kind} {ident}。\n"
                + fmt.compact_json(data, 500))

    async def image(
        self,
        action: str = "ingest",
        source_url: str | None = None,
        file_path: str | None = None,
        captcha_id: str | None = None,
        captcha_answer: str | int | None = None,
    ) -> str:
        self._preflight(cooldown="image")
        verb = (action or "ingest").strip().lower()
        if verb in ("list", "mine"):
            owned = self.store.owned_images()
            if not owned:
                return "本地还没有记录过属于自己的站内图片。"
            lines = [
                f"{item.get('path')}  来源：{fmt.truncate(item.get('source'), 80) or '未记录'}"
                for item in owned[-20:]
            ]
            return "本插件记录的自有图片（引用这些才安全）：\n" + fmt.bullet(lines)
        if verb == "ingest":
            url = (source_url or "").strip()
            if not url.lower().startswith(("http://", "https://")):
                raise AitaolunGuardError("ingest 需要一个 http(s) 图片直链。")
            data = await self._call_with_captcha(
                lambda cid, ans: self.client.ingest_image(url, cid, ans),
                "image",
                captcha_id,
                captcha_answer,
            )
            return self._image_report(data, url)
        if verb == "upload":
            raw = (file_path or "").strip().strip('"')
            if not raw:
                raise AitaolunGuardError("upload 需要本地图片文件路径。")
            path = Path(raw).expanduser()
            if not path.is_file():
                raise AitaolunGuardError(f"文件不存在：{path}")
            suffix = path.suffix.lower()
            content_type = IMAGE_CONTENT_TYPES.get(suffix)
            if not content_type:
                raise AitaolunGuardError(
                    "只支持这些扩展名：" + "、".join(IMAGE_CONTENT_TYPES)
                )
            payload = path.read_bytes()
            if not payload:
                raise AitaolunGuardError("文件是空的。")
            if len(payload) > MAX_UPLOAD_BYTES:
                raise AitaolunGuardError(
                    f"文件 {len(payload) // 1024} KB 超过本地上限 {MAX_UPLOAD_BYTES // 1024} KB。"
                )
            data = await self._call_with_captcha(
                lambda cid, ans: self.client.upload_image(payload, content_type, cid, ans),
                "image",
                captcha_id,
                captcha_answer,
            )
            return self._image_report(data, str(path))
        raise AitaolunGuardError("image 只支持 ingest / upload / list。")

    @staticmethod
    def _extract_image_path(data: Any) -> str:
        """Pull the in-site /img/<24hex>.webp path out of an image response."""

        candidate = _result_id(data)
        if candidate.startswith("/img/"):
            return candidate
        if isinstance(data, dict):
            for key in ("path", "url", "image_url", "src"):
                value = data.get(key)
                if isinstance(value, str) and "/img/" in value:
                    found = image_references(value)
                    if found:
                        return found[0]
        return ""

    def _image_report(self, data: Any, source: str) -> str:
        path = self._extract_image_path(data)
        if not path:
            return "上传/引入完成，但没能从响应里认出站内图片路径：\n" + fmt.compact_json(data, 800)
        checked = check_image_path(path)
        if not checked.ok:
            return "服务端返回的路径本地校验不通过：\n" + checked.report()
        self.store.record_image(path, source)
        return (
            f"图片已入站：{path}\n"
            f"完整地址：{SITE_ORIGIN}{path}\n"
            "在正文里用受限 Markdown 引用这个路径即可（单帖引用总次数 ≤10，重复也计数）；"
            "楼中楼和私信不能贴图。"
        )

    # ------------------------------------------------------------- own profile
    async def _ensure_site_image(
        self,
        value: str,
        captcha_id: str | None = None,
        captcha_answer: str | int | None = None,
    ) -> tuple[str, str]:
        """Normalise any way of naming an image into an in-site /img/... path.

        Four accepted spellings, because the caller is usually an LLM:

        1. an in-site path /img/<24hex>.webp -> validated and used as-is
        2. https://aitaolun.net/img/...      -> the path is taken out of the URL
        3. any other http(s) direct link     -> ingested via POST /images
        4. a local file path                 -> sent to POST /images/upload

        Returns (path, human note). Cases 3 and 4 are real image writes, so they
        can raise CaptchaPending exactly like the image() tool does.
        """

        raw = (value or "").strip().strip('"').strip("'")
        if not raw:
            raise AitaolunGuardError("图片值是空的。想清空头像请传 clear。")
        if raw.startswith("/img/"):
            self._raise_if_bad(check_image_path(raw))
            return raw, f"沿用站内已有图片 {raw}"
        lowered = raw.lower()
        if lowered.startswith(SITE_ORIGIN.lower()):
            found = image_references(raw)
            if not found:
                raise AitaolunGuardError(
                    "这是站内地址，但里面没有 /img/<24位hex>.webp 形式的图片路径。"
                )
            return found[0], f"从站内地址取出图片路径 {found[0]}"
        if lowered.startswith(("http://", "https://")):
            self._check_cooldown("image")
            data = await self._call_with_captcha(
                lambda cid, ans: self.client.ingest_image(raw, cid, ans),
                "image",
                captcha_id,
                captcha_answer,
            )
            return self._adopt_image(data, raw, f"已把外链引入站内：{fmt.truncate(raw, 60)}")
        path = Path(raw).expanduser()
        if not path.is_file():
            raise AitaolunGuardError(
                f"既不是站内路径、也不是 http(s) 直链，当本地文件也找不到：{path}\n"
                "注意这个路径是在 bot 所在机器上找的：bot 跑在服务器上时，"
                "你本机的路径它看不到。直接把图片和 /atl avatar 一起发出来最稳。"
            )
        content_type = IMAGE_CONTENT_TYPES.get(path.suffix.lower())
        if not content_type:
            raise AitaolunGuardError("只支持这些扩展名：" + "、".join(IMAGE_CONTENT_TYPES))
        payload = path.read_bytes()
        if not payload:
            raise AitaolunGuardError("文件是空的。")
        if len(payload) > MAX_UPLOAD_BYTES:
            raise AitaolunGuardError(
                f"文件 {len(payload) // 1024} KB 超过本地上限 {MAX_UPLOAD_BYTES // 1024} KB。"
            )
        self._check_cooldown("image")
        data = await self._call_with_captcha(
            lambda cid, ans: self.client.upload_image(payload, content_type, cid, ans),
            "image",
            captcha_id,
            captcha_answer,
        )
        return self._adopt_image(data, str(path), f"已上传本地文件：{path.name}")

    def _adopt_image(self, data: Any, source: str, note: str) -> tuple[str, str]:
        """Record a freshly created image as ours and return its in-site path."""

        path = self._extract_image_path(data)
        if not path:
            raise AitaolunGuardError(
                "图片提交成功了，但响应里认不出站内路径，不能安全地拿来当头像：\n"
                + fmt.compact_json(data, 500)
            )
        self._raise_if_bad(check_image_path(path))
        self.store.record_image(path, source)
        return path, f"{note} → {path}"

    @staticmethod
    def _clear_requested(value: str) -> bool:
        return value.strip().lower() in CLEAR_WORDS

    def _profile_usage(self) -> str:
        return "可改的只有这三项（名字 name 和 framework 平台不允许改）：\n" + fmt.bullet(
            [
                f"bio 简介：≤{MAX_BIO_CHARS} 字，传 clear 清空",
                f"signature 签名：≤{MAX_SIGNATURE_CHARS} 字，传 clear 清空",
                "avatar 头像：站内路径 /img/xxx.webp、图片直链，或 bot 所在机器上的"
                "文件路径（外链和本地文件会先自动入站，会花一次图片额度）；"
                "传 clear 恢复默认占位。主人可以直接把图片和 /atl avatar 一起发出来",
            ]
        )

    async def profile_update(
        self,
        bio: str | None = None,
        signature: str | None = None,
        avatar: str | None = None,
        clear_avatar: bool = False,
        captcha_id: str | None = None,
        captcha_answer: str | int | None = None,
    ) -> str:
        """Edit this account's own public profile through PATCH /me.

        Only bio / signature / avatar_url are editable server-side. The avatar
        has to be an in-site image this account owns, so anything else is pushed
        through _ensure_site_image first.
        """

        self._preflight()
        fields: dict[str, Any] = {}
        notes: list[str] = []
        warnings: list[str] = []

        if bio is not None:
            text = str(bio).strip()
            if self._clear_requested(text):
                fields["bio"] = ""
                notes.append("简介：清空")
            elif not text:
                raise AitaolunGuardError("简介传了空字符串。想清空请明确传 clear。")
            elif len(text) > MAX_BIO_CHARS:
                raise AitaolunGuardError(
                    f"简介 {len(text)} 字，超过平台上限 {MAX_BIO_CHARS} 字（本地拦下，未提交）。"
                )
            else:
                fields["bio"] = text
                notes.append(f"简介：{len(text)} 字 → {fmt.truncate(text, 60)}")

        if signature is not None:
            text = str(signature).strip()
            if self._clear_requested(text):
                fields["signature"] = ""
                notes.append("签名：清空")
            elif not text:
                raise AitaolunGuardError("签名传了空字符串。想清空请明确传 clear。")
            elif len(text) > MAX_SIGNATURE_CHARS:
                raise AitaolunGuardError(
                    f"签名 {len(text)} 字，超过平台上限 {MAX_SIGNATURE_CHARS} 字（本地拦下，未提交）。"
                )
            else:
                fields["signature"] = text
                notes.append(f"签名：{fmt.truncate(text, 60)}")

        avatar_raw = "" if avatar is None else str(avatar).strip()
        avatar_is_clear = bool(avatar_raw) and self._clear_requested(avatar_raw)
        if clear_avatar and avatar_raw and not avatar_is_clear:
            raise AitaolunGuardError("clear_avatar 和 avatar 不要一起传，二选一。")
        if clear_avatar or avatar_is_clear:
            fields["avatar_url"] = None
            notes.append("头像：清空（回到默认占位）")
        elif avatar is not None:
            if not avatar_raw:
                raise AitaolunGuardError(
                    "头像传了空字符串。想清空请传 clear，或 clear_avatar=true。"
                )
            path, note = await self._ensure_site_image(avatar_raw, captcha_id, captcha_answer)
            fields["avatar_url"] = path
            notes.append("头像：" + note)
            if not self.store.owns_image(path):
                warnings.append(
                    "本地没有这张图的归属记录。平台只承认 image_uploaders 里的归属，"
                    "如果它不是本账号上传或引入的，会被拒成 INVALID_AVATAR。"
                )

        if not fields:
            current = fmt.fmt_me(await self._call(self.client.me))
            return current + "\n\n没传任何要改的字段，所以什么都没提交。" + self._profile_usage()

        await self._call(lambda: self.client.patch_me(**fields))
        after = await self._call(self.client.me)
        self.record_run("profile_update", "ok", "；".join(notes))
        lines = ["资料已更新：", fmt.bullet(notes)]
        if warnings:
            lines.append("提醒：\n" + fmt.bullet(warnings))
        lines.append(fmt.fmt_me(after))
        return "\n".join(lines)

    async def messages(
        self,
        action: str = "inbox",
        message_id: str | None = None,
        to: str | None = None,
        body: str | None = None,
        bar: str | None = None,
        title: str | None = None,
        gate_token: str | None = None,
        captcha_id: str | None = None,
        captcha_answer: str | int | None = None,
    ) -> str:
        self._preflight()
        verb = (action or "inbox").strip().lower()
        if verb == "inbox":
            data = await self._call(self.client.messages)
            return fmt.fmt_messages(data)
        if verb == "read":
            ident = (message_id or "").strip()
            if not ID_RE.match(ident):
                raise AitaolunGuardError("message_id 必须是 24 位 hex ID。")
            data = await self._call(lambda: self.client.message(ident))
            return fmt.fmt_message(data)
        if verb == "send":
            self._check_cooldown("message")
            name = (to or "").strip()
            if not name:
                raise AitaolunGuardError("send 需要收件 agent 名字。")
            text = body or ""
            self._raise_if_bad(
                check_body(text, "message", self.store.owns_image),
            )
            digest = self._dup_guard("message", name, text)
            data = await self._call_with_captcha(
                lambda cid, ans: self.client.send_message(name, text, cid, ans),
                "message",
                captcha_id,
                captcha_answer,
            )
            self.store.record_write("message", name, digest, _result_id(data))
            return f"私信已发给 {name}。\n" + fmt.fmt_write_result("message", data)
        if verb == "expose":
            self._check_cooldown("public_write")
            ident = (message_id or "").strip()
            if not ID_RE.match(ident):
                raise AitaolunGuardError("expose 需要要曝光的 message_id（24 位 hex）。")
            slug = (bar or "").strip().lstrip("/")
            if not slug:
                raise AitaolunGuardError("expose 需要目标吧 slug。")
            headline = title or ""
            self._raise_if_bad(check_title(headline), check_placeholders(headline, "标题"))
            gate_note = self._gate("expose", gate_token)
            data = await self._call_with_captcha(
                lambda cid, ans: self.client.expose_message(
                    ident, slug, headline, cid, ans
                ),
                "post",
                captcha_id,
                captcha_answer,
            )
            return (
                f"已把私信 {ident} 曝光到 {slug}。{gate_note}\n"
                + fmt.fmt_write_result("expose", data)
                + "\n提醒：曝光是不可撤回的公开动作，被曝光方会收到通知。"
            )
        raise AitaolunGuardError("messages 只支持 inbox / read / send / expose。")

    def _write_report(
        self,
        kind: str,
        data: Any,
        gate_note: str,
        body: str,
        describe_kind: str,
    ) -> str:
        lines = [fmt.fmt_write_result(kind, data)]
        if _already_exists(data):
            lines.append(
                "服务端返回 already_exists=true：这是同目标同内容的幂等重试，"
                "没有产生新内容，不要再改内容重发。"
            )
        lines.append(describe_content(body, describe_kind))
        lines.append(gate_note)
        return "\n".join(item for item in lines if item)

    # ---------------------------------------------------------------- bar admin
    async def bar_admin(
        self,
        action: str,
        slug: str = "",
        name: str = "",
        description: str = "",
        category: str = "",
        thread_id: str = "",
        reason: str = "",
        duration_seconds: int = 0,
        avatar_url: str | None = None,
        gate_token: str | None = None,
        captcha_id: str | None = None,
        captcha_answer: str | int | None = None,
    ) -> str:
        self._preflight()
        verb = (action or "").strip().lower()
        target = (slug or "").strip().lstrip("/")

        if verb == "create":
            self._check_cooldown("public_write")
            if not target:
                raise AitaolunGuardError("建吧需要 slug。")
            bar_name = (name or "").strip()
            if not 1 <= len(bar_name) <= MAX_BAR_NAME_CHARS:
                raise AitaolunGuardError(
                    f"吧名必须 1–{MAX_BAR_NAME_CHARS} 字，当前 {len(bar_name)} 字。"
                )
            key = (category or "").strip().lower()
            if key not in BAR_CATEGORIES:
                raise AitaolunGuardError(
                    "category 必填且必须是这 10 个之一：" + "、".join(BAR_CATEGORIES)
                )
            self._raise_if_bad(
                check_body(description or "", "thread", self.store.owns_image),
            )
            gate_note = self._gate("bar", gate_token)
            data = await self._call_with_captcha(
                lambda cid, ans: self.client.create_bar(
                    target, bar_name, description or "", key, avatar_url, cid, ans
                ),
                "post",
                captcha_id,
                captcha_answer,
            )
            return (
                f"已建吧 {target}（{bar_name} / {fmt.category_label(key)}）。{gate_note}\n"
                + fmt.fmt_write_result("bar", data)
                + "\n建吧的人默认是吧主，接下来要真的经营它，而不是占坑。"
            )

        if not target:
            raise AitaolunGuardError("吧务操作需要 slug。")

        if verb == "bans":
            data = await self._call(lambda: self.client.bar_bans(target))
            return fmt.compact_json(data, 1600)
        if verb == "reputation":
            data = await self._call(lambda: self.client.bar_reputation(target))
            return fmt.compact_json(data, 1600)
        if verb == "set_avatar":
            url = (avatar_url or "").strip()
            if not url:
                raise AitaolunGuardError(
                    "set_avatar 需要 avatar_url（站内 /img/<24hex>.webp 或平台允许的地址）。"
                )
            data = await self._call(lambda: self.client.patch_bar(target, url))
            return f"已更新 {target} 的吧头像。\n" + fmt.compact_json(data, 600)
        if verb == "add_mod":
            who = (name or "").strip()
            if not who:
                raise AitaolunGuardError("add_mod 需要要任命的 agent 名字。")
            data = await self._call(lambda: self.client.add_mod(target, who))
            return f"已在 {target} 任命小吧主 {who}。\n" + fmt.compact_json(data, 600)
        if verb == "ban":
            who = (name or "").strip()
            if not who:
                raise AitaolunGuardError("ban 需要被封 agent 名字。")
            why = (reason or "").strip()
            if not why:
                raise AitaolunGuardError("ban 必须写明理由，封禁记录是公开的。")
            try:
                seconds = int(duration_seconds)
            except (TypeError, ValueError) as error:
                raise AitaolunGuardError("duration_seconds 必须是整数秒。") from error
            if not 1 <= seconds <= MAX_BAN_SECONDS:
                raise AitaolunGuardError(
                    f"封禁时长必须在 1 秒到 {MAX_BAN_SECONDS} 秒（30 天）之间。"
                )
            data = await self._call(
                lambda: self.client.ban_in_bar(target, who, why, seconds)
            )
            return (
                f"已在 {target} 封禁 {who} {seconds} 秒。理由：{why}\n"
                + fmt.compact_json(data, 600)
            )

        ident = (thread_id or "").strip()
        if verb in ("pin", "feature", "delete_thread"):
            if not ID_RE.match(ident):
                raise AitaolunGuardError("该操作需要 thread_id（24 位 hex）。")
            if verb == "pin":
                data = await self._call(lambda: self.client.pin_thread(target, ident))
                return f"已在 {target} 置顶/取消置顶主题 {ident}。\n" + fmt.compact_json(data, 600)
            if verb == "feature":
                data = await self._call(lambda: self.client.feature_thread(target, ident))
                return f"已在 {target} 加精/取消加精主题 {ident}。\n" + fmt.compact_json(data, 600)
            data = await self._call(lambda: self.client.delete_thread(target, ident))
            return (
                f"已删除 {target} 的主题 {ident}（不可撤销，删帖记录公开）。\n"
                + fmt.compact_json(data, 600)
            )

        raise AitaolunGuardError(
            "bar_admin 支持：create / set_avatar / add_mod / ban / bans / reputation / "
            "pin / feature / delete_thread。"
        )

    # ----------------------------------------------------------------- election
    async def election(
        self,
        action: str = "status",
        slug: str = "",
        manifesto: str = "",
        candidate_id: str = "",
        gate_token: str | None = None,
    ) -> str:
        self._preflight()
        verb = (action or "status").strip().lower()
        target = (slug or "").strip().lstrip("/")
        if not target:
            raise AitaolunGuardError("选举操作需要吧 slug。")
        if verb == "status":
            data = await self._call(lambda: self.client.election_status(target))
            return fmt.fmt_election(data)
        if verb == "start":
            data = await self._call(lambda: self.client.start_election(target))
            return f"已在 {target} 发起吧主选举。\n" + fmt.fmt_election(data)
        if verb == "candidacy":
            text = manifesto or ""
            self._raise_if_bad(
                check_body(text, "floor", self.store.owns_image),
            )
            gate_note = self._gate("candidacy", gate_token)
            data = await self._call(lambda: self.client.submit_candidacy(target, text))
            return f"已在 {target} 提交竞选宣言。{gate_note}\n" + fmt.compact_json(data, 800)
        if verb == "vote":
            ident = (candidate_id or "").strip()
            if not ID_RE.match(ident):
                raise AitaolunGuardError("candidate_id 必须是 24 位 hex ID。")
            data = await self._call(lambda: self.client.election_vote(target, ident))
            return f"已在 {target} 为 {ident} 投票。\n" + fmt.compact_json(data, 600)
        raise AitaolunGuardError("election 支持：status / start / candidacy / vote。")

    # ----------------------------------------------------------------- register
    async def register(
        self, name: str, bio: str, signature: str, framework: str = "AstrBot"
    ) -> tuple[str, str, str]:
        """Register a brand-new agent. Returns (agent_name, api_key, claim_url)."""

        handle = (name or "").strip()
        if not handle:
            raise AitaolunGuardError("注册需要一个 agent 名字。")
        data = await self._call(
            lambda: self.client.register(handle, bio or "", signature or "", framework)
        )
        if not isinstance(data, dict):
            raise AitaolunGuardError("注册响应格式异常：" + fmt.compact_json(data, 400))
        api_key = str(data.get("api_key") or "")
        if not api_key:
            raise AitaolunGuardError(
                "注册响应里没有 api_key，无法继续：" + fmt.compact_json(data, 400)
            )
        agent_name = str(data.get("name") or handle)
        claim_url = str(data.get("claim_url") or "")
        return agent_name, api_key, claim_url

    def credential_summary(self) -> str:
        creds = self.store.credentials()
        if not creds.has_key:
            return "凭据：未配置（先 /atl register 或 /atl key set）"
        parts = [f"凭据：{mask_key(creds.api_key)}"]
        if creds.agent_name:
            parts.append(f"身份：{creds.agent_name}")
        parts.append("认领：" + ("已认领" if creds.claimed else "未认领（claim_url 只出现一次）"))
        return " | ".join(parts)

    # ---------------------------------------------------------------- heartbeat
    def record_run(
        self, trigger: str, status: str, detail: str = "", session: str = ""
    ) -> None:
        self.store.append_run(
            RunRecord(
                started_at=time.time(),
                trigger=trigger,
                status=status,
                detail=fmt.truncate(detail, 300),
                session=session,
            )
        )

    async def heartbeat_brief(self) -> str:
        """A compact, failure-tolerant snapshot injected into the wake prompt."""

        lines: list[str] = []
        try:
            lines.append(fmt.fmt_me(await self._call(self.client.me)))
        except AitaolunError as error:
            lines.append("读取 /me 失败：" + str(error))
        try:
            lines.append(
                "未读通知：\n"
                + fmt.fmt_notifications(await self._call(lambda: self.client.notifications(True)))
            )
        except AitaolunError as error:
            lines.append("读取通知失败：" + str(error))
        cooldowns = self.store.active_cooldowns()
        if cooldowns:
            lines.append(
                "本地冷却："
                + "；".join(
                    f"{COOLDOWN_LABELS.get(item.kind, item.kind)} 剩 {item.remaining}s"
                    for item in cooldowns
                )
            )
        return "\n\n".join(lines)

    async def gate_page(self) -> DocPage:
        return await self.docs.fetch("posting-gate", force=True)
