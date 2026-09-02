"""Plugin entry point: lifecycle, /atl dispatch and permission gates.

main.py uses relative imports, so it is loaded as part of the plugin package.
No network is touched: the HTTP client is created but never used, because every
subcommand exercised here is local.
"""

import asyncio
import importlib
import sys
from pathlib import Path
from types import SimpleNamespace

PLUGIN_DIR = Path(__file__).resolve().parents[1]
if str(PLUGIN_DIR.parent) not in sys.path:
    sys.path.insert(0, str(PLUGIN_DIR.parent))

main = importlib.import_module(PLUGIN_DIR.name + ".main")

API_KEY = "atl_" + "k" * 40


def run(coro):
    return asyncio.run(coro)


class DummyToolManager:
    def __init__(self):
        self.removed = []

    def remove_func(self, name):
        self.removed.append(name)


class DummyContext:
    def __init__(self):
        self.tools = []
        self.manager = DummyToolManager()

    def add_llm_tools(self, *tools):
        self.tools.extend(tools)

    def get_llm_tool_manager(self):
        return self.manager


class FakeEvent:
    def __init__(
        self,
        admin=True,
        private=True,
        platform="aiocqhttp",
        group_id="",
        message_str="",
        chain=None,
    ):
        self._chain = list(chain or [])
        self._admin = admin
        self._private = private
        self._platform = platform
        self._group_id = group_id
        self.session_id = "sess-1"
        self.unified_msg_origin = "%s:FriendMessage:sess-1" % platform
        self.replies = []
        # 唤醒前缀在进入过滤器之前就已经被 AstrBot 剥掉了，这里模拟剥完的原文
        self.message_str = message_str
        self.is_at_or_wake_command = True
        self._extras = {}

    def get_message_str(self):
        return self.message_str

    def get_messages(self):
        return self._chain

    def set_extra(self, key, value):
        self._extras[key] = value

    def get_extra(self, key=None, default=None):
        if key is None:
            return self._extras
        return self._extras.get(key, default)

    def is_admin(self):
        return self._admin

    def is_private_chat(self):
        return self._private

    def get_platform_name(self):
        return self._platform

    def get_message_type(self):
        return SimpleNamespace(value="FriendMessage")

    def get_self_id(self):
        return "bot-1"

    def get_sender_id(self):
        return "owner-1"

    def get_sender_name(self):
        return "主人"

    def get_group_id(self):
        return self._group_id

    def plain_result(self, text):
        self.replies.append(text)
        return text


def make_plugin(tmp_path, monkeypatch, config=None):
    monkeypatch.setattr(
        main.StarTools, "get_data_dir", staticmethod(lambda name=None: tmp_path)
    )
    context = DummyContext()
    plugin = main.AitaolunPlugin(context, config if config is not None else {})
    return plugin, context


def ready(plugin, *, key=True, bind=True):
    if key:
        plugin.store.set_api_key(API_KEY, "测试机")
    if bind:
        plugin.store.bind_session("aiocqhttp:FriendMessage:sess-1", "主人")
    return plugin


def dispatch(plugin, sub, rest=None, event=None):
    return run(plugin._dispatch(event or FakeEvent(), sub, rest or []))


# ----------------------------------------------------------------- lifecycle


def test_initialize_registers_every_tool_then_terminate_removes_them(
    tmp_path, monkeypatch
):
    plugin, context = make_plugin(tmp_path, monkeypatch)

    async def scenario():
        await plugin.initialize()
        assert len(context.tools) == len(main.tool_names())
        assert plugin.service is not None
        assert plugin.scheduler is not None and plugin.scheduler.running
        await plugin.terminate()

    run(scenario())
    assert sorted(context.manager.removed) == sorted(main.tool_names())
    assert plugin.scheduler is None


def test_configured_api_key_migrates_into_the_private_data_dir(tmp_path, monkeypatch):
    plugin, _ = make_plugin(tmp_path, monkeypatch, {"api_key": API_KEY})

    async def scenario():
        await plugin.initialize()
        await plugin.terminate()

    run(scenario())
    assert plugin.store.credentials().api_key == API_KEY
    assert plugin.store.credentials_path.is_file()


def test_helpers_before_initialize_report_instead_of_crashing(tmp_path, monkeypatch):
    plugin, _ = make_plugin(tmp_path, monkeypatch)
    for helper in (plugin._svc, plugin._sched):
        try:
            helper()
        except main.AitaolunError:
            pass
        else:
            raise AssertionError("uninitialised access must raise a typed error")


# ------------------------------------------------------------------ dispatch


def wired(tmp_path, monkeypatch, config=None):
    """A plugin with services built but the scheduler loop not started."""

    plugin, context = make_plugin(tmp_path, monkeypatch, config)
    plugin.client = SimpleNamespace(close=None)
    plugin.docs = main.DocFetcher()
    plugin.gate = main.PostingGate(docs=plugin.docs)
    plugin.service = main.AitaolunService(
        client=plugin.client,
        store=plugin.store,
        gate=plugin.gate,
        docs=plugin.docs,
        options=dict(config or {}),
    )
    plugin.scheduler = main.HeartbeatScheduler(
        store=plugin.store, config=config or {}, runner=plugin._wake
    )
    return plugin, context


def test_help_is_the_default_and_unknown_subcommands_show_it(tmp_path, monkeypatch):
    plugin, _ = wired(tmp_path, monkeypatch)
    assert dispatch(plugin, "help") == main.HELP_TEXT
    unknown = dispatch(plugin, "nonsense")
    assert "未知子指令" in unknown and main.HELP_TEXT in unknown


def test_aliases_only_point_at_implemented_subcommands():
    known = {
        "help",
        "h",
        "?",
        "status",
        "register",
        "claim",
        "key",
        "bind",
        "unbind",
        "heartbeat",
        "wake",
        "skill",
        "sync",
        "pause",
        "resume",
        "runs",
        "gate",
        "whoami",
        "persona",
        "diag",
        "bio",
        "sign",
        "signature",
        "avatar",
        "feed",
        "thread",
        "bars",
        "docs",
        "memory",
        "stats",
        "shot",
    }
    body = (PLUGIN_DIR / "main.py").read_text(encoding="utf-8")
    for alias, target in main.AitaolunPlugin._ALIASES.items():
        assert target == target.lower(), alias
        assert target in known, f"{alias} -> {target}"
    # 白名单里的每个子指令都真的在 _dispatch 里被判断过
    for sub in known:
        assert f'"{sub}"' in body, sub


def test_status_never_prints_the_raw_key(tmp_path, monkeypatch):
    plugin, _ = wired(tmp_path, monkeypatch)
    ready(plugin)
    text = dispatch(plugin, "status")
    assert API_KEY not in text
    assert "atl_kkkk" in text
    assert str(tmp_path) in text


def test_key_subcommand_roundtrip(tmp_path, monkeypatch):
    plugin, _ = wired(tmp_path, monkeypatch)

    assert "用法" in dispatch(plugin, "key", ["set"])
    saved = dispatch(plugin, "key", ["set", API_KEY])
    assert API_KEY not in saved
    assert plugin.store.credentials().api_key == API_KEY

    shown = dispatch(plugin, "key", ["show"])
    assert API_KEY not in shown
    assert "credentials.json" in shown

    assert "弃号" in dispatch(plugin, "key", ["clear"])
    assert not plugin.store.credentials().has_key
    assert "用法" in dispatch(plugin, "key", ["bogus"])


def test_credential_operations_require_admin_and_private_chat(tmp_path, monkeypatch):
    plugin, _ = wired(tmp_path, monkeypatch)
    for sub in ("register", "claim", "key"):
        for event in (FakeEvent(admin=False), FakeEvent(private=False)):
            try:
                dispatch(plugin, sub, ["x"], event)
            except PermissionError:
                pass
            else:
                raise AssertionError("%s must be gated" % sub)


def test_register_refuses_to_overwrite_an_existing_identity(tmp_path, monkeypatch):
    plugin, _ = wired(tmp_path, monkeypatch)
    ready(plugin)
    text = dispatch(plugin, "register", ["新名字"])
    assert "key clear" in text
    assert API_KEY not in text
    assert "用法" in dispatch(plugin, "register", [])


def test_claim_flow(tmp_path, monkeypatch):
    plugin, _ = wired(tmp_path, monkeypatch)
    assert "还没有注册过" in dispatch(plugin, "claim")

    creds = main.Credentials(
        api_key=API_KEY, agent_name="测试机", claim_url="https://aitaolun.net/claim/xyz"
    )
    plugin.store.save_credentials(creds)
    assert "https://aitaolun.net/claim/xyz" in dispatch(plugin, "claim")

    assert "已标记为已认领" in dispatch(plugin, "claim", ["done"])
    assert plugin.store.credentials().claimed
    assert plugin.store.credentials().claim_url == ""
    assert "已经标记为已认领" in dispatch(plugin, "claim")


def test_bind_records_the_session_on_any_platform(tmp_path, monkeypatch):
    """未来任务不经过消息管道，所以绑定哪个平台都一样，不再有 aiocqhttp 限制。"""

    plugin, context = wired(tmp_path, monkeypatch, {"heartbeat_enabled": True})
    with_cron(plugin, context)
    text = dispatch(plugin, "bind")
    state = plugin.store.scheduler_state()
    assert plugin.store.bound_session() == "aiocqhttp:FriendMessage:sess-1"
    assert state["platform"] == "aiocqhttp"
    assert state["msg_type"] == "FriendMessage"
    assert state["self_id"] == "bot-1"
    assert "返场调度已开启" in text
    assert "⚠" not in text

    other = dispatch(plugin, "bind", event=FakeEvent(platform="telegram"))
    assert "telegram:FriendMessage:sess-1" in other
    assert "⚠" not in other
    assert plugin.store.scheduler_state()["platform"] == "telegram"

    assert "已解绑" in dispatch(plugin, "unbind")
    assert plugin.store.bound_session() == ""


def test_bind_warns_when_the_scheduler_is_switched_off(tmp_path, monkeypatch):
    plugin, _ = wired(tmp_path, monkeypatch, {"heartbeat_enabled": False})
    assert "heartbeat_enabled" in dispatch(plugin, "bind")


def test_manual_trigger_needs_a_binding_and_a_key(tmp_path, monkeypatch):
    plugin, _ = wired(tmp_path, monkeypatch)
    assert "先在目标会话里执行 /atl bind" in dispatch(plugin, "heartbeat")

    ready(plugin, key=False)
    assert "还没有 api_key" in dispatch(plugin, "heartbeat")


def test_pause_and_resume_respect_the_ban_latch(tmp_path, monkeypatch):
    plugin, _ = wired(tmp_path, monkeypatch)
    assert "已暂停返场：睡一会" in dispatch(plugin, "pause", ["睡一会"])
    assert plugin.scheduler.paused()

    plugin.store.set_platform_banned(True, "刷屏")
    refused = dispatch(plugin, "resume")
    assert "拒绝恢复" in refused and "刷屏" in refused
    assert plugin.scheduler.paused()

    forced = dispatch(plugin, "resume", ["--force"])
    assert "强制清除封禁闩锁" in forced
    assert plugin.store.platform_ban() is None
    assert not plugin.scheduler.paused()


def test_status_surfaces_the_ban_latch_and_cooldowns(tmp_path, monkeypatch):
    plugin, _ = wired(tmp_path, monkeypatch)
    ready(plugin)
    plugin.store.set_platform_banned(True, "刷屏")
    plugin.store.set_cooldown("public_write", 60, "PUBLIC_RATE_LIMITED")
    text = dispatch(plugin, "status")
    assert "平台封禁闩锁已生效" in text
    assert "public_write 剩" in text


def test_runs_and_memory_and_gate_are_local_only(tmp_path, monkeypatch):
    plugin, _ = wired(tmp_path, monkeypatch)
    assert "还没有返场记录" in dispatch(plugin, "runs")
    plugin.service.record_run("heartbeat", "cron_armed", "试跑", "sess")
    assert "试跑" in dispatch(plugin, "runs")

    plugin.service.memory("write", "persona", "爱吵架")
    assert "爱吵架" in dispatch(plugin, "memory", ["persona"])

    gate_text = dispatch(plugin, "gate")
    assert "闸门强制：开启" in gate_text
    assert "指令这里不会给你 token" in gate_text


def test_docs_without_an_argument_lists_pages_offline(tmp_path, monkeypatch):
    plugin, _ = wired(tmp_path, monkeypatch)
    text = dispatch(plugin, "docs")
    assert "posting-gate" in text and "skill" in text


def test_wake_without_a_binding_records_the_reason(tmp_path, monkeypatch):
    plugin, _ = wired(tmp_path, monkeypatch)
    run(plugin._wake("heartbeat", "prompt"))
    assert plugin.store.scheduler_state()["last_error"] == "未绑定会话"


# ------------------------------------------------------------ argument parsing


def drive(plugin, event):
    """跑真正的 /atl 指令入口（异步生成器），返回它 yield 出来的文本。"""

    async def collect():
        return [item async for item in plugin.atl_command(event)]

    return run(collect())


def test_parse_arg_line_keeps_every_token_after_the_command_head():
    cases = {
        "atl register 爱丽丝": "register 爱丽丝",
        "/atl register 爱丽丝": "register 爱丽丝",
        "！atl   register    爱丽丝  ": "register 爱丽丝",
        "爱讨论 注册 爱丽丝": "注册 爱丽丝",
        "atl": "",
        "/atl": "",
        "爱讨论": "",
        "atl memory write notes 今天吧里很吵": "memory write notes 今天吧里很吵",
    }
    for raw, expected in cases.items():
        assert main.parse_arg_line(raw) == expected, raw


def test_parse_arg_line_falls_back_when_the_raw_text_is_unusable():
    assert main.parse_arg_line("", "register 爱丽丝") == "register 爱丽丝"
    assert main.parse_arg_line(None, None) == ""
    # 只是碰巧以 atl 开头的普通词，不算指令头，退回框架给的值
    assert main.parse_arg_line("atlas 很大", "status") == "status"


def test_astrbot_command_filter_has_nothing_left_to_swallow():
    """真·AstrBot 过滤器跑一遍：处理函数不声明参数，框架分词就吃不掉东西。

    历史 bug：`args: GreedyStr = ""` 因为带了默认值，CommandFilter 把默认值而不是
    注解记进 handler_params，贪婪判定失效，`/atl register 爱丽丝` 只传了 "register"。
    """

    from astrbot.core.star.filter.command import CommandFilter

    cmd_filter = CommandFilter(
        "atl",
        alias={"爱讨论"},
        handler_md=SimpleNamespace(handler=main.AitaolunPlugin.atl_command),
    )
    assert cmd_filter.handler_params == {}

    event = FakeEvent(message_str="atl register 爱丽丝")
    assert cmd_filter.filter(event, {}) is True
    assert event.get_extra("parsed_params") == {}
    assert main.parse_arg_line(event.get_message_str()) == "register 爱丽丝"


def test_atl_command_hands_the_agent_name_to_register(tmp_path, monkeypatch):
    plugin, _ = wired(tmp_path, monkeypatch)
    calls = []

    async def fake_register(name, bio, signature, framework):
        calls.append((name, bio, signature, framework))
        return name, API_KEY, "https://aitaolun.net/claim/abc"

    monkeypatch.setattr(plugin.service, "register", fake_register)
    replies = drive(plugin, FakeEvent(message_str="atl register 爱丽丝"))

    assert [call[0] for call in calls] == ["爱丽丝"]
    assert "用法：/atl register" not in replies[0]
    assert "注册成功：爱丽丝" in replies[0]
    assert API_KEY not in replies[0]
    assert plugin.store.credentials().agent_name == "爱丽丝"


def test_register_keeps_spaces_inside_the_agent_name(tmp_path, monkeypatch):
    plugin, _ = wired(tmp_path, monkeypatch)
    seen = []

    async def fake_register(name, bio, signature, framework):
        seen.append(name)
        return name, API_KEY, ""

    monkeypatch.setattr(plugin.service, "register", fake_register)
    drive(plugin, FakeEvent(message_str="atl 注册 Alice B"))
    assert seen == ["Alice B"]


def test_atl_command_handles_the_chinese_head_bare_calls_and_typos(
    tmp_path, monkeypatch
):
    plugin, _ = wired(tmp_path, monkeypatch)
    ready(plugin)

    assert "爱讨论插件状态" in drive(plugin, FakeEvent(message_str="爱讨论 状态"))[0]
    assert drive(plugin, FakeEvent(message_str="atl"))[0] == main.HELP_TEXT
    assert "未知子指令" in drive(plugin, FakeEvent(message_str="atl 胡说八道"))[0]


def test_atl_command_reports_permission_errors_instead_of_raising(
    tmp_path, monkeypatch
):
    plugin, _ = wired(tmp_path, monkeypatch)
    event = FakeEvent(admin=False, message_str="atl register 爱丽丝")
    assert "只有 AstrBot 管理员" in drive(plugin, event)[0]

    event = FakeEvent(private=False, message_str="atl register 爱丽丝")
    assert "只能在私聊里做" in drive(plugin, event)[0]


# --------------------------------------------------------- future-task waking


class FakeCronManager:
    """假的 context.cron_manager：只记参数，字段名和真的一致。"""

    def __init__(self, jobs=None, boom=False):
        self.calls = []
        self.deleted = []
        self.jobs = list(jobs or [])
        self.boom = boom
        self._started = True

    async def add_active_job(self, **kwargs):
        if self.boom:
            raise RuntimeError("数据库挂了")
        self.calls.append(kwargs)
        job = SimpleNamespace(job_id="job-%d" % (len(self.calls),), name=kwargs["name"])
        self.jobs.append(job)
        return job

    async def list_jobs(self, job_type=None):
        return list(self.jobs)

    async def delete_job(self, job_id):
        self.deleted.append(job_id)
        self.jobs = [job for job in self.jobs if job.job_id != job_id]


def with_cron(plugin, context, **kwargs):
    manager = FakeCronManager(**kwargs)
    context.cron_manager = manager
    return manager


def forbid_injection(monkeypatch):
    """伪造消息进管道那条路已经删掉了；谁把它加回来，这两个桩就让测试红。"""

    async def refuse(*args, **kwargs):
        raise AssertionError("返场不许伪造消息进消息管道")

    monkeypatch.setattr(main.StarTools, "create_message", staticmethod(refuse))
    monkeypatch.setattr(main.StarTools, "create_event", staticmethod(refuse))


def test_wake_arms_a_future_task_when_the_framework_supports_it(tmp_path, monkeypatch):
    plugin, context = wired(tmp_path, monkeypatch, {"heartbeat_include_brief": False})
    manager = with_cron(plugin, context)
    dispatch(plugin, "bind")
    forbid_injection(monkeypatch)

    run(plugin._wake("heartbeat", "该返场了"))

    payload = manager.calls[0]["payload"]
    assert payload["session"] == "aiocqhttp:FriendMessage:sess-1"
    assert payload["note"] == "该返场了"
    assert payload["sender_id"] == "owner-1"
    assert manager.calls[0]["run_once"] is True

    record = plugin.store.runs(1)[0]
    assert record.status == "cron_armed" and "job-1" in record.detail
    assert plugin.store.scheduler_state()["last_error"] == ""


def test_a_framework_without_future_tasks_skips_the_round_and_says_why(
    tmp_path, monkeypatch
):
    """没有 cron_manager 就没有唤醒办法：如实记一条，不许绕路。"""

    plugin, _ = wired(tmp_path, monkeypatch, {"heartbeat_include_brief": False})
    dispatch(plugin, "bind")
    forbid_injection(monkeypatch)

    run(plugin._wake("heartbeat", "该返场了"))

    record = plugin.store.runs(1)[0]
    assert record.status == "cron_unavailable"
    assert "cron_manager" in record.detail
    assert "cron_manager" in plugin.store.scheduler_state()["last_error"]


def test_an_unarmable_future_task_is_recorded_instead_of_worked_around(
    tmp_path, monkeypatch
):
    plugin, context = wired(tmp_path, monkeypatch, {"heartbeat_include_brief": False})
    with_cron(plugin, context, boom=True)
    dispatch(plugin, "bind")
    forbid_injection(monkeypatch)

    run(plugin._wake("heartbeat", "该返场了"))

    record = plugin.store.runs(1)[0]
    assert record.status == "cron_failed" and "数据库挂了" in record.detail


def test_wake_status_rereads_the_framework_every_time(tmp_path, monkeypatch):
    """插件热重载后 cron_manager 会换一个对象，状态不能缓存。"""

    plugin, context = wired(tmp_path, monkeypatch)
    ready_now, note = plugin._wake_status()
    assert ready_now is False and "cron_manager" in note

    with_cron(plugin, context)
    ready_now, note = plugin._wake_status()
    assert ready_now is True and "未来任务" in note


def test_manual_trigger_reports_the_future_task(tmp_path, monkeypatch):
    plugin, context = wired(
        tmp_path, monkeypatch, {"heartbeat_include_brief": False, "heartbeat_enabled": True}
    )
    with_cron(plugin, context)
    ready(plugin)
    dispatch(plugin, "bind")

    text = dispatch(plugin, "heartbeat")
    assert "已排入 AstrBot 未来任务" in text
    assert "send_message_to_user" in text
    assert "未来任务" in dispatch(plugin, "bind")
    assert "未来任务" in dispatch(plugin, "status")


def test_diag_reports_the_future_task_route(tmp_path, monkeypatch):
    plugin, context = wired(tmp_path, monkeypatch)
    with_cron(plugin, context)
    dispatch(plugin, "bind")

    text = dispatch(plugin, "diag")
    assert "AstrBot 未来任务：可用" in text
    assert "调度器已启动" in text
    assert "待触发任务：0 个" in text
    # 走未来任务时唤醒判定完全无关，别再拿它误导人
    assert "会被唤醒" not in text
    assert "atl_*" in text


def test_stale_future_tasks_are_purged_on_load_and_unload(tmp_path, monkeypatch):
    plugin, context = make_plugin(tmp_path, monkeypatch)
    leftovers = [
        SimpleNamespace(job_id="dead-1", name="aitaolun_heartbeat"),
        SimpleNamespace(job_id="keep", name="user_daily_report"),
    ]
    manager = with_cron(plugin, context, jobs=leftovers)

    async def scenario():
        await plugin.initialize()
        assert manager.deleted == ["dead-1"]
        await plugin.terminate()

    run(scenario())
    assert manager.deleted == ["dead-1"]  # 卸载时已经没有残留可删
    assert [job.job_id for job in manager.jobs] == ["keep"]


def test_purge_failures_never_block_startup(tmp_path, monkeypatch):
    plugin, context = make_plugin(tmp_path, monkeypatch)

    class BrokenManager(FakeCronManager):
        async def list_jobs(self, job_type=None):
            raise RuntimeError("库锁了")

    context.cron_manager = BrokenManager()

    async def scenario():
        await plugin.initialize()
        assert plugin.scheduler is not None and plugin.scheduler.running
        await plugin.terminate()

    run(scenario())


def test_diag_says_plainly_that_a_framework_without_future_tasks_cannot_wake(
    tmp_path, monkeypatch
):
    """没有 cron_manager 的框架：诊断必须直说排不出去，别给假希望。"""

    plugin, _ = wired(tmp_path, monkeypatch)
    assert "先在你想让它说话的那个会话里执行 /atl bind" in dispatch(plugin, "diag")

    dispatch(plugin, "bind")
    text = dispatch(plugin, "diag")
    assert "返场诊断" in text
    assert "aiocqhttp:FriendMessage:sess-1" in text
    assert "AstrBot 未来任务：不可用" in text
    assert "会话串能否解析：✅" in text
    assert "还没跑过" in text
    # 唤醒判定、唤醒前缀这些概念已经和返场无关了，别再出现在诊断里
    assert "唤醒前缀" not in text and "会被唤醒" not in text


def test_bind_warns_up_front_when_the_framework_cannot_schedule(tmp_path, monkeypatch):
    plugin, _ = wired(tmp_path, monkeypatch, {"heartbeat_enabled": True})
    text = dispatch(plugin, "bind")
    assert "已把返场绑定到当前会话" in text
    assert "⚠" in text and "cron_manager" in text
    assert "/atl heartbeat" in text
    # 状态页同样标红，而不是若无其事地报一条"唤醒方式"
    assert "⚠ 返场唤醒" in dispatch(plugin, "status")

def test_persona_lists_all_four_layers(tmp_path, monkeypatch):
    plugin, _ = wired(tmp_path, monkeypatch)
    text = dispatch(plugin, "persona")
    for marker in ("人格情景", "/atl bio", "atl_memory", "heartbeat_prompt"):
        assert marker in text
    assert "用内置默认" in text

    plugin, _ = wired(tmp_path, monkeypatch, {"heartbeat_prompt": "只逛技术吧"})
    assert "已自定义" in dispatch(plugin, "persona")


def test_profile_subcommands_require_admin_and_forward_the_whole_line(
    tmp_path, monkeypatch
):
    plugin, _ = wired(tmp_path, monkeypatch)
    calls = []

    async def fake_update(**kwargs):
        calls.append(kwargs)
        return "ok"

    monkeypatch.setattr(plugin.service, "profile_update", fake_update)

    assert dispatch(plugin, "bio", ["一个", "爱吵架的", "bot"]) == "ok"
    assert dispatch(plugin, "sign", ["嘴", "很", "碎"]) == "ok"
    assert dispatch(plugin, "signature", ["嘴碎"]) == "ok"
    assert dispatch(plugin, "avatar", ["/img/" + "a" * 24 + ".webp"]) == "ok"
    assert calls == [
        {"bio": "一个 爱吵架的 bot"},
        {"signature": "嘴 很 碎"},
        {"signature": "嘴碎"},
        {"avatar": "/img/" + "a" * 24 + ".webp"},
    ]

    # 不带参数 = 查看当前资料和用法
    calls.clear()
    assert dispatch(plugin, "bio") == "ok"
    assert calls == [{}]

    for sub in ("bio", "sign", "avatar"):
        try:
            dispatch(plugin, sub, ["x"], FakeEvent(admin=False))
        except PermissionError:
            pass
        else:
            raise AssertionError("%s must require admin" % sub)


def test_collect_images_prefers_the_attached_image_over_the_quoted_one():
    from astrbot.api.message_components import Image, Plain, Reply

    attached = Image.fromURL("https://example.invalid/new.png")
    quoted = Image.fromURL("https://example.invalid/old.png")
    reply = Reply(id=1, chain=[Plain("看这张"), quoted])

    assert main.collect_images([]) == []
    assert main.collect_images(None) == []
    assert main.collect_images([Plain("只有文字")]) == []
    assert main.collect_images([reply]) == [quoted]
    assert main.collect_images([Plain("换成这个"), attached]) == [attached]
    # 同时带图又引用带图的消息时，自己发的那张排在前面
    assert main.collect_images([reply, attached]) == [attached, quoted]
    # 只往下看一层，不会无限递归
    assert main.collect_images([Reply(id=2, chain=[Reply(id=3, chain=[quoted])])]) == []


def test_avatar_takes_the_image_sent_along_with_the_command(tmp_path, monkeypatch):
    from astrbot.api.message_components import Image, Plain

    plugin, _ = wired(tmp_path, monkeypatch)
    calls = []

    async def fake_update(**kwargs):
        calls.append(kwargs)
        return "ok"

    monkeypatch.setattr(plugin.service, "profile_update", fake_update)

    async def fake_convert(self):
        return "/srv/tmp/downloaded.png"

    monkeypatch.setattr(Image, "convert_to_file_path", fake_convert, raising=False)

    sent = Image.fromURL("https://qq.invalid/avatar.png")
    event = FakeEvent(chain=[Plain("/atl avatar"), sent])
    assert dispatch(plugin, "avatar", [], event) == "ok"
    assert calls == [{"avatar": "/srv/tmp/downloaded.png"}]

    # 写了参数就以参数为准，图片不参与
    calls.clear()
    dispatch(plugin, "avatar", ["clear"], event)
    assert calls == [{"avatar": "clear"}]

    # 既没参数也没图 = 查看当前头像
    calls.clear()
    dispatch(plugin, "avatar", [], FakeEvent())
    assert calls == [{}]


def test_avatar_reports_a_download_failure_instead_of_crashing(tmp_path, monkeypatch):
    from astrbot.api.message_components import Image

    plugin, _ = wired(tmp_path, monkeypatch)

    async def boom(self):
        raise OSError("QQ 图床超时")

    monkeypatch.setattr(Image, "convert_to_file_path", boom, raising=False)

    event = FakeEvent(chain=[Image.fromURL("https://qq.invalid/x.png")])
    try:
        dispatch(plugin, "avatar", [], event)
    except main.AitaolunError as error:
        assert "取不到你发的那张图" in str(error)
        assert "OSError" in str(error)
    else:
        raise AssertionError("download failures must surface as AitaolunError")


def test_avatar_survives_events_without_a_message_chain(tmp_path, monkeypatch):
    plugin, _ = wired(tmp_path, monkeypatch)
    calls = []

    async def fake_update(**kwargs):
        calls.append(kwargs)
        return "ok"

    monkeypatch.setattr(plugin.service, "profile_update", fake_update)

    event = FakeEvent()
    monkeypatch.setattr(
        event, "get_messages", lambda: (_ for _ in ()).throw(RuntimeError("no chain"))
    )
    assert dispatch(plugin, "avatar", [], event) == "ok"
    assert calls == [{}]
