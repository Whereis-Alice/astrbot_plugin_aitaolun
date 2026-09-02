"""Wake the agent through AstrBot's own scheduler instead of faking a message.

`StarTools.create_event(is_wake=True)` does not really wake anything: the
pipeline's WakingCheckStage judges the synthetic message all over again and only
accepts a wake prefix, an @ of the bot itself, or a private chat that is allowed
to skip the prefix. Anything else is dropped in the very first stage — from the
outside that looks exactly like "注入成功了但 bot 毫无反应".

AstrBot already ships the right door, the one its own FutureTaskTool uses::

    await context.cron_manager.add_active_job(
        name=..., cron_expression=None, payload={"session": umo, "note": ...},
        run_once=True, run_at=<datetime>,
    )

That is a one-shot "未来任务". When it fires, the framework builds the main agent
itself (人格 + 完整工具集 + 该会话的历史), runs the agent loop to the end and
writes the result back into the conversation. No pipeline, no wake judgement, no
prefix games, and the job row deletes itself afterwards.

The heartbeat keeps its own timer — arbitrary interval, jitter and the platform
ban latch all live there — and when a slot comes due it arms exactly one future
task a couple of seconds out. So the framework always does the waking, and the
note we hand it is freshly built (站内快照不会过期).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

#: 所有由本插件排出去的任务都用这个前缀命名，方便清理时认出自己的东西，
#: 不会碰到用户在 WebUI 里手工建的定时任务。
JOB_PREFIX = "aitaolun_"

#: 排任务和真正触发之间留一点点余量。APScheduler 的 misfire_grace_time 是 30s，
#: 所以这里给 2 秒既足够安全，又不会让 /atl heartbeat 感觉在等。
DEFAULT_DELAY_SECONDS = 2.0

#: WebUI 的「定时任务」页面在任务排队的那几秒里显示的名字。
JOB_LABELS: dict[str, str] = {
    "heartbeat": "爱讨论返场",
    "skill_update": "爱讨论每日规则同步",
    "manual": "爱讨论返场（手动触发）",
}
FALLBACK_LABEL = "爱讨论返场"


class CronUnavailable(RuntimeError):
    """这个 AstrBot 装不出未来任务（版本太老或者 cron_manager 没起来）。"""


def job_name(trigger: str) -> str:
    """把触发原因变成一个安全的任务名，始终带上插件前缀。"""

    raw = str(trigger or "wake").strip().lower()
    slug = "".join(ch if (ch.isalnum() or ch == "_") else "_" for ch in raw)
    return JOB_PREFIX + (slug or "wake")


class CronWaker:
    """`context.cron_manager` 的薄封装：只做「排一次性未来任务」这一件事。

    故意不持有 Context：调用方每次现取 `cron_manager`，插件热重载后也不会拿着
    一个失效的引用。
    """

    def __init__(self, manager: Any = None) -> None:
        self.manager = manager

    # ------------------------------------------------------------- capability
    @property
    def available(self) -> bool:
        """这个框架版本能不能排未来任务。"""

        return callable(getattr(self.manager, "add_active_job", None))

    @property
    def started(self) -> bool:
        """调度器是否已经被 core_lifecycle 启动。

        插件的 `initialize()` 比 `cron_manager.start()` 早（core_lifecycle 先
        `plugin_manager.reload()` 再 `_load()`），所以这一位在加载的最初几秒是
        False。这期间**不能**排任务：`_schedule_job` 会自己把 APScheduler 起来并
        置上 `_started`，等框架真的调 `start()` 时就会提前 return，连带跳过
        `sync_from_db()` —— 别人存在库里的定时任务全都不会被排。宁可这一轮回退。
        """

        return bool(getattr(self.manager, "_started", False))

    # -------------------------------------------------------------------- arm
    async def arm(
        self,
        *,
        trigger: str,
        session: str,
        note: str,
        sender_id: str = "",
        delay_seconds: float = DEFAULT_DELAY_SECONDS,
    ) -> str:
        """排一个一次性未来任务，返回框架给的 job_id。

        `sender_id` 传主人的平台 ID：框架会拿它和 `admins_id` 比对，命中就把这次
        唤醒当成管理员发起的，需要管理员权限的工具才不会被拦。
        """

        if not self.available:
            raise CronUnavailable(
                "这个 AstrBot 版本没有 cron_manager，排不了未来任务。"
            )
        if not self.started:
            raise CronUnavailable(
                "AstrBot 的定时调度器还没启动（框架刚起来的几秒），这一轮先不排任务。"
            )
        target = str(session or "").strip()
        if not target:
            raise CronUnavailable("没有绑定会话，未来任务不知道该在哪儿说话。")
        try:
            delay = max(0.0, float(delay_seconds))
        except (TypeError, ValueError):
            delay = DEFAULT_DELAY_SECONDS
        payload: dict[str, Any] = {
            "session": target,
            "note": str(note or ""),
            "origin": "aitaolun",
            "trigger": str(trigger or "heartbeat"),
        }
        if sender_id:
            payload["sender_id"] = str(sender_id)
        job = await self.manager.add_active_job(
            name=job_name(trigger),
            cron_expression=None,
            payload=payload,
            description=JOB_LABELS.get(str(trigger), FALLBACK_LABEL),
            enabled=True,
            persistent=True,
            run_once=True,
            run_at=datetime.now(timezone.utc) + timedelta(seconds=delay),
        )
        return str(getattr(job, "job_id", "") or "")

    # ------------------------------------------------------------ housekeeping
    async def pending(self) -> list[Any]:
        """还挂在框架任务表里、属于本插件的任务。

        正常情况下这里是空的：任务只在触发前的几秒钟存在，跑完框架自己删。
        留下残留只有两种情况——进程正好死在那几秒里，或者插件被重载了。
        """

        lister = getattr(self.manager, "list_jobs", None)
        if lister is None:
            return []
        try:
            jobs = await lister("active_agent")
        except TypeError:  # 老签名不吃 job_type
            jobs = await lister()
        return [
            job
            for job in (jobs or [])
            if str(getattr(job, "name", "") or "").startswith(JOB_PREFIX)
        ]

    async def purge(self) -> int:
        """删掉所有残留任务，返回删掉的个数。

        插件加载和卸载时各调一次：残留任务要么已经过期（APScheduler 判成 misfire
        直接跳过，行只会一直躺在库里），要么指向一个已经不存在的工具集，留着没有
        任何意义。
        """

        deleter = getattr(self.manager, "delete_job", None)
        if deleter is None:
            return 0
        removed = 0
        for job in await self.pending():
            job_id = str(getattr(job, "job_id", "") or "")
            if not job_id:
                continue
            await deleter(job_id)
            removed += 1
        return removed


__all__ = [
    "DEFAULT_DELAY_SECONDS",
    "JOB_LABELS",
    "JOB_PREFIX",
    "CronUnavailable",
    "CronWaker",
    "job_name",
]
