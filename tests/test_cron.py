"""One-shot future tasks: the only honest way to wake our own agent.

Nothing here talks to APScheduler directly — CronWaker is a thin wrapper over
`context.cron_manager`, so a fake manager that records its kwargs is enough to
pin down the contract we depend on (run_once + run_at + a parseable session).
"""

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from aitaolun.cron import JOB_PREFIX, CronUnavailable, CronWaker, job_name

SESSION = "aiocqhttp:FriendMessage:sess-1"


def run(coro):
    return asyncio.run(coro)


class FakeCronManager:
    """记录参数的假 cron_manager，字段名和真的一一对应。"""

    def __init__(self, jobs=None, started=True):
        self.calls = []
        self.deleted = []
        self.jobs = list(jobs or [])
        self._started = started
        self._seq = 0

    async def add_active_job(self, **kwargs):
        self.calls.append(kwargs)
        self._seq += 1
        job = SimpleNamespace(job_id=f"job-{self._seq}", name=kwargs["name"])
        self.jobs.append(job)
        return job

    async def list_jobs(self, job_type=None):
        self.last_job_type = job_type
        return list(self.jobs)

    async def delete_job(self, job_id):
        self.deleted.append(job_id)
        self.jobs = [job for job in self.jobs if job.job_id != job_id]


def test_job_names_are_prefixed_and_sanitised():
    assert job_name("heartbeat") == JOB_PREFIX + "heartbeat"
    assert job_name("skill_update") == JOB_PREFIX + "skill_update"
    # 空的、带空格和标点的都要变成安全名字，且不会丢掉前缀
    assert job_name("") == JOB_PREFIX + "wake"
    assert job_name(" Manual Run! ") == JOB_PREFIX + "manual_run_"


def test_availability_and_started_are_pure_capability_checks():
    assert CronWaker(None).available is False
    assert CronWaker(None).started is False
    assert CronWaker(SimpleNamespace()).available is False

    manager = FakeCronManager(started=False)
    waker = CronWaker(manager)
    assert waker.available is True
    assert waker.started is False
    manager._started = True
    assert waker.started is True


def test_arm_books_a_one_shot_job_a_few_seconds_out():
    manager = FakeCronManager()
    waker = CronWaker(manager)

    job_id = run(
        waker.arm(
            trigger="heartbeat",
            session=SESSION,
            note="该返场了",
            sender_id="owner-1",
            delay_seconds=2.0,
        )
    )

    assert job_id == "job-1"
    call = manager.calls[0]
    assert call["name"] == JOB_PREFIX + "heartbeat"
    # 必须是一次性任务：跑完框架自己删，不会留下一个每分钟叫醒它的东西
    assert call["run_once"] is True
    assert call["cron_expression"] is None
    assert call["persistent"] is True and call["enabled"] is True
    assert call["description"] == "爱讨论返场"

    run_at = call["run_at"]
    assert isinstance(run_at, datetime) and run_at.tzinfo is not None
    now = datetime.now(timezone.utc)
    assert now < run_at <= now + timedelta(seconds=5)

    payload = call["payload"]
    # session 必须是框架能 MessageSession.from_str 的三段式
    assert payload["session"] == SESSION and payload["session"].count(":") == 2
    assert payload["note"] == "该返场了"
    assert payload["origin"] == "aitaolun" and payload["trigger"] == "heartbeat"
    # sender_id 传过去框架才可能把这次唤醒当成管理员发起的
    assert payload["sender_id"] == "owner-1"


def test_arm_omits_sender_id_when_unknown_and_clamps_a_silly_delay():
    manager = FakeCronManager()
    waker = CronWaker(manager)

    run(
        waker.arm(
            trigger="skill_update", session=SESSION, note="重读文档", delay_seconds=-9
        )
    )

    call = manager.calls[0]
    assert "sender_id" not in call["payload"]
    assert call["description"] == "爱讨论每日规则同步"
    assert call["run_at"] <= datetime.now(timezone.utc) + timedelta(seconds=1)


def test_arm_refuses_without_a_manager_or_a_session():
    for waker, session in ((CronWaker(None), SESSION), (CronWaker(FakeCronManager()), "  ")):
        try:
            run(waker.arm(trigger="heartbeat", session=session, note="x"))
        except CronUnavailable as error:
            assert str(error)
        else:  # pragma: no cover - 失败时才会走到
            raise AssertionError("arm 本该抛 CronUnavailable")


def test_arm_refuses_while_the_framework_scheduler_is_not_up_yet():
    """插件 initialize 比 cron_manager.start 早，这几秒排任务会顶掉框架的 sync_from_db。"""

    manager = FakeCronManager(started=False)
    try:
        run(CronWaker(manager).arm(trigger="heartbeat", session=SESSION, note="x"))
    except CronUnavailable as error:
        assert "调度器" in str(error)
    else:  # pragma: no cover - 失败时才会走到
        raise AssertionError("调度器没起来就不该排任务")
    assert manager.calls == []


def test_pending_only_counts_our_own_jobs():
    manager = FakeCronManager(
        jobs=[
            SimpleNamespace(job_id="a", name=JOB_PREFIX + "heartbeat"),
            SimpleNamespace(job_id="b", name="user_daily_report"),
        ]
    )
    waker = CronWaker(manager)

    pending = run(waker.pending())

    assert [job.job_id for job in pending] == ["a"]
    # 只翻 active_agent 那一类，不去碰用户自己建的 basic 任务
    assert manager.last_job_type == "active_agent"


def test_pending_falls_back_when_list_jobs_takes_no_arguments():
    class OldManager(FakeCronManager):
        async def list_jobs(self):  # 老签名
            return list(self.jobs)

    manager = OldManager(jobs=[SimpleNamespace(job_id="a", name=JOB_PREFIX + "manual")])
    assert [job.job_id for job in run(CronWaker(manager).pending())] == ["a"]

    assert run(CronWaker(SimpleNamespace()).pending()) == []


def test_purge_deletes_only_the_leftovers_it_owns():
    manager = FakeCronManager(
        jobs=[
            SimpleNamespace(job_id="a", name=JOB_PREFIX + "heartbeat"),
            SimpleNamespace(job_id="b", name="user_daily_report"),
            SimpleNamespace(job_id="", name=JOB_PREFIX + "broken"),
        ]
    )
    waker = CronWaker(manager)

    assert run(waker.purge()) == 1
    assert manager.deleted == ["a"]
    assert [job.name for job in manager.jobs] == ["user_daily_report", JOB_PREFIX + "broken"]
    assert run(CronWaker(SimpleNamespace()).purge()) == 0
