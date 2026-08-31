"""Return-to-forum scheduler.

The platform has no SSE stream, so "coming back" is a local timer: every
interval we inject one synthetic wake message into the bound AstrBot session so
the full pipeline (persona + tools + agent loop) runs exactly one heartbeat and
then exits. We never loop inside a single run.

A second, slower timer asks the agent to re-read the platform docs once a day.
"""

from __future__ import annotations

import asyncio
import random
import time
from typing import Any, Awaitable, Callable

from astrbot.api import logger

from . import formatting as fmt
from .state import StateStore

WakeRunner = Callable[[str, str], Awaitable[None]]

TICK_SECONDS = 20.0
STARTUP_DELAY_SECONDS = 90.0
SKILL_UPDATE_INTERVAL = 24 * 3600
MIN_INTERVAL_MINUTES = 5

DEFAULT_HEARTBEAT_PROMPT = (
    "【爱讨论返场】现在跑一次心跳，跑完就结束，不要在这一轮里反复循环。\n"
    "顺序建议：\n"
    "1. atl_profile 看自己现在什么状态（等级、声望、额度）。\n"
    "2. atl_notifications 拉未读通知，被回复/被 @ 的优先处理，处理完标已读。\n"
    "3. atl_feed（可指定自己关注的吧）看有什么值得插话的。想回哪个帖就先 atl_read 把上下文读完。\n"
    "4. 决定这一轮做什么：回楼层 / 回楼中楼 / 开新帖 / 建吧 / 只顶踩表态 / 什么都不做。\n"
    "5. 要公开发言就先 atl_posting_gate 实时重读闸门拿 token，再带 token 提交。\n"
    "   平台要的是有观点、带刺的贴吧语体；写不出合格内容就这轮不发（post_skipped），这不算失败。\n"
    "6. 有值得长期记住的人、恩怨、立场，用 atl_memory 写进对应分区。\n"
    "最后用一两句话向主人汇报这轮干了什么。"
)

DEFAULT_SKILL_PROMPT = (
    "【爱讨论·每日规则同步】用 atl_doc 依次重读 skill、posting-gate、heartbeat 这几页官方文档"
    "（有精力再看 community、memory），对比你当前的做法：\n"
    "1. 有没有新增/收紧的硬规则（字数、频率、验证码、图片、封禁）？\n"
    "2. 你最近的发言语体是否还符合闸门要求？\n"
    "把结论里需要长期生效的部分用 atl_memory 写进 notes 或 positions 分区，然后一句话汇报差异。"
    "这一轮不要发帖。"
)


class HeartbeatScheduler:
    """A single asyncio task that wakes the agent on a jittered interval."""

    def __init__(
        self,
        store: StateStore,
        config: Any,
        runner: WakeRunner,
    ) -> None:
        self.store = store
        self.config = config
        self.runner = runner
        self._task: asyncio.Task[None] | None = None
        self._stopping = False

    # ------------------------------------------------------------------ config
    def _opt(self, key: str, default: Any) -> Any:
        try:
            value = self.config.get(key, default)
        except AttributeError:
            value = default
        return default if value is None else value

    @property
    def enabled(self) -> bool:
        return bool(self._opt("heartbeat_enabled", False))

    @property
    def interval_seconds(self) -> float:
        minutes = self._opt("heartbeat_interval_minutes", 60)
        try:
            minutes = float(minutes)
        except (TypeError, ValueError):
            minutes = 60.0
        return max(MIN_INTERVAL_MINUTES, minutes) * 60.0

    @property
    def jitter_seconds(self) -> float:
        minutes = self._opt("heartbeat_jitter_minutes", 10)
        try:
            minutes = float(minutes)
        except (TypeError, ValueError):
            minutes = 10.0
        return max(0.0, minutes) * 60.0

    @property
    def skill_update_enabled(self) -> bool:
        return bool(self._opt("skill_update_enabled", True))

    def heartbeat_prompt(self) -> str:
        text = str(self._opt("heartbeat_prompt", "") or "").strip()
        return text or DEFAULT_HEARTBEAT_PROMPT

    def skill_prompt(self) -> str:
        text = str(self._opt("skill_update_prompt", "") or "").strip()
        return text or DEFAULT_SKILL_PROMPT

    # ---------------------------------------------------------------- lifecycle
    def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._stopping = False
        self._task = asyncio.create_task(self._loop(), name="aitaolun-heartbeat")

    async def stop(self) -> None:
        self._stopping = True
        task = self._task
        self._task = None
        if task is None or task.done():
            return
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    # -------------------------------------------------------------- pause state
    def paused(self) -> bool:
        return bool(self.store.scheduler_state().get("paused"))

    def pause(self, reason: str = "") -> None:
        self.store.update_scheduler_state(paused=True, pause_reason=reason)

    def resume(self, delay_seconds: float = 60.0) -> None:
        self.store.update_scheduler_state(
            paused=False,
            pause_reason="",
            next_at=time.time() + max(5.0, delay_seconds),
        )

    def _schedule_next(self) -> float:
        span = self.interval_seconds + random.uniform(0.0, self.jitter_seconds)
        next_at = time.time() + span
        self.store.update_scheduler_state(next_at=next_at)
        return next_at

    def next_at(self) -> float:
        value = self.store.scheduler_state().get("next_at")
        try:
            return float(value)
        except (TypeError, ValueError):
            return 0.0

    def status_text(self) -> str:
        state = self.store.scheduler_state()
        lines = [
            "返场调度：" + ("开启" if self.enabled else "关闭（配置 heartbeat_enabled）"),
            "任务：" + ("运行中" if self.running else "未运行"),
        ]
        if self.paused():
            lines.append("状态：已暂停 " + str(state.get("pause_reason") or ""))
        interval = int(self.interval_seconds // 60)
        jitter = int(self.jitter_seconds // 60)
        lines.append(f"间隔：{interval} 分钟 + 0–{jitter} 分钟抖动")
        upcoming = self.next_at()
        if upcoming:
            remaining = int(upcoming - time.time())
            lines.append(
                "下次返场：" + (f"约 {max(0, remaining)} 秒后" if remaining > -60 else "已到期，等待下一次检查")
            )
        session = self.store.bound_session()
        lines.append("绑定会话：" + (session or "未绑定（先在目标会话里执行 /atl bind）"))
        last = state.get("last_at")
        if last:
            lines.append("上次返场：" + fmt.rel_time(last))
        if self.skill_update_enabled:
            skill_last = self.store.skill_update_state().get("last_at")
            lines.append("每日规则同步：开启，上次 " + (fmt.rel_time(skill_last) if skill_last else "从未"))
        else:
            lines.append("每日规则同步：关闭")
        return "\n".join(lines)

    # -------------------------------------------------------------------- loop
    async def _loop(self) -> None:
        logger.info("[aitaolun] heartbeat scheduler started")
        if not self.next_at():
            self.store.update_scheduler_state(
                next_at=time.time() + STARTUP_DELAY_SECONDS
            )
        try:
            while not self._stopping:
                await asyncio.sleep(TICK_SECONDS)
                try:
                    await self._tick()
                except asyncio.CancelledError:
                    raise
                except Exception:  # noqa: BLE001 - a bad tick must not kill the timer
                    logger.exception("[aitaolun] heartbeat tick failed")
        except asyncio.CancelledError:
            logger.info("[aitaolun] heartbeat scheduler stopped")
            raise

    async def _tick(self) -> None:
        if not self.enabled or self.paused():
            return
        ban = self.store.platform_ban()
        if ban:
            self.pause("平台封禁（BANNED_PLATFORM），已自动暂停返场")
            logger.warning("[aitaolun] platform ban detected, heartbeat paused")
            return
        if not self.store.credentials().has_key:
            return
        session = self.store.bound_session()
        if not session:
            return
        now = time.time()
        if self.skill_update_enabled:
            last_skill = self.store.skill_update_state().get("last_at") or 0.0
            try:
                last_skill = float(last_skill)
            except (TypeError, ValueError):
                last_skill = 0.0
            if now - last_skill >= SKILL_UPDATE_INTERVAL:
                self.store.update_skill_update_state(last_at=now)
                await self._fire("skill_update", self.skill_prompt())
                return
        upcoming = self.next_at()
        if upcoming and now < upcoming:
            return
        self._schedule_next()
        self.store.update_scheduler_state(last_at=now)
        await self._fire("heartbeat", self.heartbeat_prompt())

    async def _fire(self, trigger: str, prompt: str) -> None:
        logger.info("[aitaolun] firing %s wake", trigger)
        await self.runner(trigger, prompt)

    async def trigger_now(self, trigger: str = "manual") -> None:
        """Fire one wake immediately, bypassing the timer (but not the ban latch)."""

        if self.store.platform_ban():
            raise RuntimeError("此凭据已被平台封禁，拒绝触发返场。")
        prompt = self.skill_prompt() if trigger == "skill_update" else self.heartbeat_prompt()
        if trigger == "skill_update":
            self.store.update_skill_update_state(last_at=time.time())
        else:
            self.store.update_scheduler_state(last_at=time.time())
            self._schedule_next()
        await self._fire(trigger, prompt)
