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
    def __init__(self, admin=True, private=True, platform="aiocqhttp", group_id=""):
        self._admin = admin
        self._private = private
        self._platform = platform
        self._group_id = group_id
        self.session_id = "sess-1"
        self.unified_msg_origin = "%s:FriendMessage:sess-1" % platform
        self.replies = []

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
        "feed",
        "thread",
        "bars",
        "docs",
        "memory",
        "stats",
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


def test_bind_records_the_session_and_warns_on_other_platforms(tmp_path, monkeypatch):
    plugin, _ = wired(tmp_path, monkeypatch, {"heartbeat_enabled": True})
    text = dispatch(plugin, "bind")
    state = plugin.store.scheduler_state()
    assert plugin.store.bound_session() == "aiocqhttp:FriendMessage:sess-1"
    assert state["platform"] == "aiocqhttp"
    assert state["msg_type"] == "FriendMessage"
    assert state["self_id"] == "bot-1"
    assert "返场调度已开启" in text
    assert "⚠" not in text

    other = dispatch(plugin, "bind", event=FakeEvent(platform="telegram"))
    assert "只支持 aiocqhttp" in other

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
    plugin.service.record_run("heartbeat", "injected", "试跑", "sess")
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
