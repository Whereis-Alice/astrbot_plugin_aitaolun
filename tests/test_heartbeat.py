"""The return-to-forum timer: one wake per tick, and never while unsafe."""

import asyncio
import time

from aitaolun.heartbeat import (
    DEFAULT_HEARTBEAT_PROMPT,
    DEFAULT_SKILL_PROMPT,
    MIN_INTERVAL_MINUTES,
    HeartbeatScheduler,
)
from aitaolun.state import StateStore

SESSION = "aiocqhttp:GroupMessage:123456"


def run(coro):
    return asyncio.run(coro)


class Fired(list):
    async def __call__(self, trigger, prompt):
        self.append((trigger, prompt))


def build(tmp_path, config=None, *, ready=True, skill_done=True):
    store = StateStore(data_dir=tmp_path)
    if ready:
        store.set_api_key("atl_" + "k" * 40, "测试机")
        store.bind_session(SESSION, "测试群")
    if skill_done:
        store.update_skill_update_state(last_at=time.time())
    options = {"heartbeat_enabled": True, "heartbeat_interval_minutes": 60}
    options.update(config or {})
    fired = Fired()
    return HeartbeatScheduler(store, options, fired), store, fired


def test_interval_and_jitter_are_clamped(tmp_path):
    scheduler, _, _ = build(
        tmp_path, {"heartbeat_interval_minutes": 1, "heartbeat_jitter_minutes": -5}
    )
    assert scheduler.interval_seconds == MIN_INTERVAL_MINUTES * 60
    assert scheduler.jitter_seconds == 0.0

    bad, _, _ = build(
        tmp_path,
        {"heartbeat_interval_minutes": "很久", "heartbeat_jitter_minutes": None},
    )
    assert bad.interval_seconds == 3600.0
    assert bad.jitter_seconds == 600.0


def test_prompts_fall_back_to_the_built_in_ones(tmp_path):
    scheduler, _, _ = build(tmp_path, {"heartbeat_prompt": "   "})
    assert scheduler.heartbeat_prompt() == DEFAULT_HEARTBEAT_PROMPT
    assert scheduler.skill_prompt() == DEFAULT_SKILL_PROMPT

    custom, _, _ = build(tmp_path, {"heartbeat_prompt": "只看不发"})
    assert custom.heartbeat_prompt() == "只看不发"


def test_a_due_tick_fires_exactly_one_heartbeat(tmp_path):
    scheduler, store, fired = build(tmp_path)
    store.update_scheduler_state(next_at=time.time() - 1)

    run(scheduler._tick())
    assert [item[0] for item in fired] == ["heartbeat"]
    assert fired[0][1] == DEFAULT_HEARTBEAT_PROMPT
    # The next slot was booked, so an immediate second tick stays quiet.
    assert scheduler.next_at() > time.time()
    run(scheduler._tick())
    assert len(fired) == 1


def test_a_tick_before_the_due_time_does_nothing(tmp_path):
    scheduler, store, fired = build(tmp_path)
    store.update_scheduler_state(next_at=time.time() + 3600)
    run(scheduler._tick())
    assert fired == []


def test_disabled_paused_keyless_or_unbound_never_fires(tmp_path):
    for config, ready in (({"heartbeat_enabled": False}, True), ({}, False)):
        scheduler, store, fired = build(tmp_path / str(ready), config, ready=ready)
        store.update_scheduler_state(next_at=time.time() - 1)
        run(scheduler._tick())
        assert fired == []

    scheduler, store, fired = build(tmp_path / "paused")
    store.update_scheduler_state(next_at=time.time() - 1)
    scheduler.pause("主人让我歇会儿")
    assert scheduler.paused()
    run(scheduler._tick())
    assert fired == []

    scheduler.resume(delay_seconds=1)
    assert not scheduler.paused()
    assert scheduler.next_at() > time.time()


def test_a_platform_ban_auto_pauses_the_timer(tmp_path):
    scheduler, store, fired = build(tmp_path)
    store.update_scheduler_state(next_at=time.time() - 1)
    store.set_platform_banned(True, "刷屏")

    run(scheduler._tick())
    assert fired == []
    assert scheduler.paused()
    assert "封禁" in str(store.scheduler_state().get("pause_reason"))


def test_daily_skill_update_takes_priority_once_per_day(tmp_path):
    scheduler, store, fired = build(tmp_path, skill_done=False)
    store.update_scheduler_state(next_at=time.time() - 1)

    run(scheduler._tick())
    assert [item[0] for item in fired] == ["skill_update"]
    # It must not also fire a heartbeat in the same tick.
    assert len(fired) == 1

    run(scheduler._tick())
    assert [item[0] for item in fired] == ["skill_update", "heartbeat"]

    run(scheduler._tick())
    assert len(fired) == 2


def test_skill_update_can_be_switched_off(tmp_path):
    scheduler, store, fired = build(
        tmp_path, {"skill_update_enabled": False}, skill_done=False
    )
    store.update_scheduler_state(next_at=time.time() - 1)
    run(scheduler._tick())
    assert [item[0] for item in fired] == ["heartbeat"]


def test_trigger_now_bypasses_the_timer_but_not_the_ban(tmp_path):
    scheduler, store, fired = build(tmp_path)
    store.update_scheduler_state(next_at=time.time() + 3600)

    run(scheduler.trigger_now())
    assert [item[0] for item in fired] == ["manual"]
    assert fired[0][1] == DEFAULT_HEARTBEAT_PROMPT
    run(scheduler.trigger_now("skill_update"))
    assert fired[1][0] == "skill_update"

    store.set_platform_banned(True, "封了")
    try:
        run(scheduler.trigger_now())
    except RuntimeError as error:
        assert "封禁" in str(error)
    else:
        raise AssertionError("a banned credential must not be able to wake")
    assert len(fired) == 2


def test_status_text_is_readable_and_mentions_the_binding(tmp_path):
    scheduler, store, _ = build(tmp_path)
    text = scheduler.status_text()
    assert "返场调度：开启" in text
    assert SESSION in text
    assert "间隔：60 分钟" in text

    unbound, store2, _ = build(tmp_path / "unbound", ready=False)
    assert "未绑定" in unbound.status_text()


def test_start_and_stop_are_idempotent(tmp_path):
    async def scenario():
        scheduler, _, _ = build(tmp_path)
        scheduler.start()
        scheduler.start()
        assert scheduler.running
        await scheduler.stop()
        assert not scheduler.running
        await scheduler.stop()

    run(scenario())
