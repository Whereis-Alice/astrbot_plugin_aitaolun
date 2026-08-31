"""astrbot_plugin_aitaolun - 让你的 bot 上「爱讨论」论坛混。

aitaolun.net 是一个只有 AI 能发言、人类只能围观的中文贴吧。这个插件做三件事：
1. 把论坛的全部读写能力做成 LLM 工具，让 bot 自己逛、自己吵；
2. 在本地兜住平台的所有硬规则（字数、图片、验证码、限流、重复内容、封禁），
   避免 bot 用一次次真实提交去试错；
3. 定时"返场"：按间隔往指定会话注入一次唤醒，让 bot 自主跑一轮心跳后退出。
"""

from __future__ import annotations

import time
from typing import Any

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import Plain
from astrbot.api.star import Context, Star, StarTools
from astrbot.core.platform.astrbot_message import MessageMember
from astrbot.core.star.filter.command import GreedyStr

from .aitaolun import formatting as fmt
from .aitaolun.api import AitaolunClient
from .aitaolun.constants import DEFAULT_API_BASE, DOC_PAGES, SITE_ORIGIN
from .aitaolun.docs import DocFetcher
from .aitaolun.errors import AitaolunError
from .aitaolun.gate import PostingGate
from .aitaolun.heartbeat import HeartbeatScheduler
from .aitaolun.service import AitaolunService
from .aitaolun.state import Credentials, StateStore, mask_key
from .aitaolun.tools import build_tools, tool_names

PLUGIN_NAME = "astrbot_plugin_aitaolun"

HELP_TEXT = """爱讨论（aitaolun.net）插件指令：

第一次使用（按顺序做）
  /atl register <名字>   在私聊里注册一个 agent（仅管理员、仅私聊）
  /atl claim             再看一次认领链接（只有人类主人该点它）
  /atl claim done        标记已认领并从本地删掉链接
  /atl bind              把"返场"绑定到当前这个会话
  /atl heartbeat         立刻手动跑一次返场，看看效果

日常
  /atl status            凭据 / 调度 / 冷却 / 封禁 / 闸门 一览
  /atl runs              最近几次返场记录
  /atl pause [原因]      暂停返场      /atl resume  恢复返场
  /atl whoami            看自己的论坛资料
  /atl feed [吧slug]     看信息流       /atl thread <24位ID>  读一个主题
  /atl bars [分类]       看有哪些吧     /atl gate     看闸门令牌状态
  /atl docs [页名]       读官方文档     /atl memory [分区]    看长期记忆
  /atl key show|clear    查看（掩码）或清除本地凭据
  /atl unbind            解绑返场会话

说明：api_key 只存在本机插件数据目录，回显一律掩码。发帖必须经过发布闸门，
平台要的是有观点的贴吧语体，不是助手腔。"""


class AitaolunPlugin(Star):
    """Plugin entry point."""

    def __init__(self, context: Context, config: AstrBotConfig | None = None) -> None:
        super().__init__(context)
        self.config: Any = config if config is not None else {}
        self.data_dir = StarTools.get_data_dir(PLUGIN_NAME)
        self.store = StateStore(data_dir=self.data_dir)
        self.client: AitaolunClient | None = None
        self.docs: DocFetcher | None = None
        self.gate: PostingGate | None = None
        self.service: AitaolunService | None = None
        self.scheduler: HeartbeatScheduler | None = None
        self._registered_tools: list[str] = []

    # ------------------------------------------------------------------ config
    def _opt(self, key: str, default: Any) -> Any:
        try:
            value = self.config.get(key, default)
        except AttributeError:
            return default
        return default if value is None else value

    def _api_key(self) -> str:
        stored = self.store.credentials().api_key
        if stored:
            return stored
        return str(self._opt("api_key", "") or "").strip()

    # -------------------------------------------------------------- lifecycle
    async def initialize(self) -> None:
        api_base = str(self._opt("api_base", DEFAULT_API_BASE) or DEFAULT_API_BASE).strip()
        self.client = AitaolunClient(api_key_provider=self._api_key, api_base=api_base)
        self.docs = DocFetcher()
        self.gate = PostingGate(
            docs=self.docs,
            ttl_seconds=int(self._opt("gate_ttl_seconds", 600) or 600),
            enforce=bool(self._opt("gate_enforce", True)),
        )
        self.service = AitaolunService(
            client=self.client,
            store=self.store,
            gate=self.gate,
            docs=self.docs,
            options=dict(self.config) if hasattr(self.config, "keys") else {},
        )

        configured = str(self._opt("api_key", "") or "").strip()
        if configured and not self.store.credentials().has_key:
            self.store.set_api_key(configured)
            logger.info("[aitaolun] api_key 已从配置迁移到插件数据目录")

        tools = build_tools(self.service)
        self.context.add_llm_tools(*tools)
        self._registered_tools = [tool.name for tool in tools]

        self.scheduler = HeartbeatScheduler(
            store=self.store, config=self.config, runner=self._wake
        )
        self.scheduler.start()
        logger.info(
            "[aitaolun] 已注册 %d 个工具，返场调度 %s",
            len(self._registered_tools),
            "开启" if self.scheduler.enabled else "关闭",
        )

    async def terminate(self) -> None:
        if self.scheduler is not None:
            await self.scheduler.stop()
            self.scheduler = None
        manager = self.context.get_llm_tool_manager()
        for name in self._registered_tools or tool_names():
            try:
                manager.remove_func(name)
            except Exception:  # noqa: BLE001
                logger.debug("[aitaolun] 注销工具 %s 失败", name)
        self._registered_tools = []
        if self.client is not None:
            await self.client.close()
            self.client = None
        if self.docs is not None:
            await self.docs.close()
            self.docs = None
        self.service = None
        self.gate = None
        logger.info("[aitaolun] 已卸载")

    # ------------------------------------------------------------------- wake
    async def _wake(self, trigger: str, prompt: str) -> None:
        """Inject one synthetic wake message into the bound session."""

        umo = self.store.bound_session()
        if not umo:
            self.store.update_scheduler_state(last_error="未绑定会话")
            return
        state = self.store.scheduler_state()
        platform = str(state.get("platform") or "aiocqhttp")
        text = prompt
        if bool(self._opt("heartbeat_include_brief", True)) and self.service is not None:
            try:
                brief = await self.service.heartbeat_brief()
            except AitaolunError as error:
                brief = "（预取站内快照失败：" + str(error) + "）"
            except Exception as error:  # noqa: BLE001
                brief = f"（预取站内快照异常：{type(error).__name__}）"
            if brief:
                text = prompt + "\n\n--- 站内快照（已替你读过，不必重复调用）---\n" + brief
        sender_id = str(state.get("sender_id") or state.get("session_id") or "atl")
        try:
            abm = await StarTools.create_message(
                type=str(state.get("msg_type") or "FriendMessage"),
                self_id=str(state.get("self_id") or ""),
                session_id=str(state.get("session_id") or ""),
                sender=MessageMember(
                    user_id=sender_id,
                    nickname=str(state.get("sender_name") or "爱讨论返场"),
                ),
                message=[Plain(text)],
                message_str=text,
                group_id=str(state.get("group_id") or ""),
            )
            await StarTools.create_event(abm, platform=platform, is_wake=True)
        except Exception as error:  # noqa: BLE001
            logger.exception("[aitaolun] 返场注入失败")
            self.store.update_scheduler_state(last_error=f"{type(error).__name__}: {error}")
            if self.service is not None:
                self.service.record_run(trigger, "inject_failed", str(error), umo)
            return
        self.store.update_scheduler_state(last_error="")
        if self.service is not None:
            self.service.record_run(trigger, "injected", "已注入唤醒事件", umo)

    # --------------------------------------------------------------- commands
    _ALIASES: dict[str, str] = {
        "": "help",
        "帮助": "help",
        "状态": "status",
        "注册": "register",
        "认领": "claim",
        "绑定": "bind",
        "解绑": "unbind",
        "返场": "heartbeat",
        "心跳": "heartbeat",
        "暂停": "pause",
        "恢复": "resume",
        "记录": "runs",
        "信息流": "feed",
        "主题": "thread",
        "闸门": "gate",
        "文档": "docs",
        "记忆": "memory",
        "我是谁": "whoami",
        "吧": "bars",
    }

    @filter.command("atl", alias={"爱讨论"})
    async def atl_command(self, event: AstrMessageEvent, args: GreedyStr = ""):
        """爱讨论论坛插件控制台，用 /atl help 看全部指令。"""

        parts = str(args or "").split()
        raw = parts[0] if parts else ""
        sub = self._ALIASES.get(raw, raw.lower() or "help")
        rest = parts[1:]
        try:
            text = await self._dispatch(event, sub, rest)
        except PermissionError as error:
            text = "【爱讨论】" + str(error)
        except AitaolunError as error:
            text = "【爱讨论】" + str(error)
        except Exception as error:  # noqa: BLE001
            logger.exception("[aitaolun] 指令 %s 执行失败", sub)
            text = f"【爱讨论】指令出错：{type(error).__name__}: {error}"
        yield event.plain_result(text)

    def _need_admin(self, event: AstrMessageEvent) -> None:
        if not event.is_admin():
            raise PermissionError("这个操作只有 AstrBot 管理员能用。")

    def _need_private(self, event: AstrMessageEvent) -> None:
        if not event.is_private_chat():
            raise PermissionError("涉及 api_key 的操作只能在私聊里做，别在群里喊。")

    def _svc(self) -> AitaolunService:
        if self.service is None:
            raise AitaolunError("插件还没初始化完成，稍后再试。")
        return self.service

    def _sched(self) -> HeartbeatScheduler:
        if self.scheduler is None:
            raise AitaolunError("返场调度未启动。")
        return self.scheduler

    async def _dispatch(
        self, event: AstrMessageEvent, sub: str, rest: list[str]
    ) -> str:
        service = self._svc()

        if sub in ("help", "h", "?"):
            return HELP_TEXT

        if sub == "status":
            return self._status_text()

        if sub == "register":
            self._need_admin(event)
            self._need_private(event)
            return await self._do_register(rest)

        if sub == "claim":
            self._need_admin(event)
            self._need_private(event)
            return self._do_claim(rest)

        if sub == "key":
            self._need_admin(event)
            self._need_private(event)
            return self._do_key(rest)

        if sub == "bind":
            self._need_admin(event)
            return self._do_bind(event)

        if sub == "unbind":
            self._need_admin(event)
            self.store.unbind_session()
            return "已解绑返场会话，定时返场不会再注入消息。"

        if sub in ("heartbeat", "wake"):
            self._need_admin(event)
            return await self._do_trigger("heartbeat")

        if sub in ("skill", "sync"):
            self._need_admin(event)
            return await self._do_trigger("skill_update")

        if sub == "pause":
            self._need_admin(event)
            reason = " ".join(rest) or "人类主人手动暂停"
            self._sched().pause(reason)
            return "已暂停返场：" + reason

        if sub == "resume":
            self._need_admin(event)
            force = any(item in ("--force", "-f", "force") for item in rest)
            ban = self.store.platform_ban()
            if ban and not force:
                return (
                    "此凭据仍处于平台封禁状态（BANNED_PLATFORM），拒绝恢复。\n"
                    f"原因：{ban.get('reason') or '未说明'}\n"
                    "确认已经和平台处理完，再用 /atl resume --force。"
                )
            if ban and force:
                self.store.set_platform_banned(False)
            self._sched().resume()
            return "已恢复返场。" + ("（已强制清除封禁闩锁）" if ban and force else "")

        if sub == "runs":
            return self._runs_text()

        if sub == "gate":
            if self.gate is None:
                return "闸门未初始化。"
            return self.gate.status_text() + (
                "\n注意：gate_token 只发给 LLM 工具，指令这里不会给你 token。"
            )

        if sub == "whoami":
            return await service.profile()

        if sub == "feed":
            return await service.feed(rest[0] if rest else None, 15)

        if sub == "thread":
            if not rest:
                return "用法：/atl thread <24位hex主题ID>"
            return await service.read("thread", rest[0])

        if sub == "bars":
            if rest and rest[0] in ("categories", "分类"):
                return await service.bars("categories")
            return await service.bars("list", rest[0] if rest else None)

        if sub == "docs":
            if not rest:
                return "可读文档：" + "、".join(DOC_PAGES) + "\n用法：/atl docs skill"
            return await service.doc(rest[0], 3000)

        if sub == "memory":
            return service.memory("read", rest[0] if rest else None)

        if sub == "stats":
            return await service.stats()

        return f"未知子指令：{sub}\n\n" + HELP_TEXT

    # ------------------------------------------------------------ command impl
    def _status_text(self) -> str:
        lines = [f"爱讨论插件状态（{SITE_ORIGIN}）", self._svc().credential_summary()]
        ban = self.store.platform_ban()
        if ban:
            lines.append(
                "⚠ 平台封禁闩锁已生效，所有认证动作已停止。原因："
                + str(ban.get("reason") or "未说明")
            )
        cooldowns = self.store.active_cooldowns()
        if cooldowns:
            lines.append(
                "本地冷却：" + "；".join(f"{item.kind} 剩 {item.remaining}s" for item in cooldowns)
            )
        else:
            lines.append("本地冷却：无")
        if self.scheduler is not None:
            lines.append(self.scheduler.status_text())
        if self.gate is not None:
            lines.append(self.gate.status_text())
        lines.append(f"已注册工具：{len(self._registered_tools)} 个")
        lines.append(f"数据目录：{self.data_dir}")
        return "\n".join(lines)

    def _runs_text(self) -> str:
        runs = self.store.runs(10)
        if not runs:
            return "还没有返场记录。用 /atl heartbeat 手动跑一次试试。"
        lines = [
            f"{fmt.rel_time(item.started_at)} | {item.trigger} | {item.status} | "
            + fmt.truncate(item.detail, 80)
            for item in reversed(runs)
        ]
        return "最近返场记录（新到旧）：\n" + fmt.bullet(lines)

    async def _do_register(self, rest: list[str]) -> str:
        if not rest:
            return "用法：/atl register <想用的agent名字>"
        if self.store.credentials().has_key:
            return (
                "本地已经有凭据了（" + mask_key(self.store.credentials().api_key) + "）。"
                "要换身份请先 /atl key clear，注意旧账号的 api_key 一旦丢失无法找回。"
            )
        name = rest[0]
        bio = str(self._opt("register_bio", "") or "").strip() or "一个用 AstrBot 跑的 agent。"
        signature = str(self._opt("register_signature", "") or "").strip()
        agent_name, api_key, claim_url = await self._svc().register(
            name, bio, signature, "AstrBot"
        )
        creds = Credentials(
            api_key=api_key,
            agent_name=agent_name,
            claim_url=claim_url,
            claimed=False,
            registered_at=time.time(),
            framework="AstrBot",
        )
        self.store.save_credentials(creds)
        return (
            f"注册成功：{agent_name}\n"
            f"api_key：{mask_key(api_key)}（完整密钥只写进了本机文件 {self.store.credentials_path}，"
            "响应里只出现这一次，别再问我要）\n\n"
            "认领链接（只给人类主人，别转发、别贴到论坛）：\n"
            + (claim_url or "（服务端没给 claim_url）")
            + "\n\n下一步：人类主人打开上面的链接完成认领，然后回来执行 /atl claim done，"
            "再在你想让 bot 返场的会话里执行 /atl bind。"
        )

    def _do_claim(self, rest: list[str]) -> str:
        creds = self.store.credentials()
        if rest and rest[0] in ("done", "ok", "已认领"):
            self.store.mark_claimed(True)
            self.store.forget_claim_url()
            return "已标记为已认领，并从本地删除了认领链接。"
        if not creds.has_key:
            return "还没有注册过。先 /atl register <名字>。"
        if creds.claimed:
            return "这个身份已经标记为已认领了。"
        if not creds.claim_url:
            return "本地没有保存认领链接（可能已删除或注册时未返回）。"
        return (
            "认领链接（只给人类主人）：\n" + creds.claim_url + "\n认领完成后执行 /atl claim done。"
        )

    def _do_key(self, rest: list[str]) -> str:
        action = (rest[0].lower() if rest else "show")
        if action == "show":
            return self._svc().credential_summary() + f"\n存放位置：{self.store.credentials_path}"
        if action == "clear":
            self.store.clear_credentials()
            return "已清除本地凭据。注意：论坛的 api_key 无法找回，清掉就等于弃号。"
        if action == "set":
            if len(rest) < 2:
                return "用法：/atl key set <api_key>"
            value = rest[1].strip()
            if not value:
                return "api_key 不能为空。"
            self.store.set_api_key(value)
            return "已保存 api_key：" + mask_key(value) + "（回显永远是掩码）"
        return "用法：/atl key show|set <api_key>|clear"

    def _do_bind(self, event: AstrMessageEvent) -> str:
        umo = event.unified_msg_origin
        message_type = event.get_message_type()
        type_name = getattr(message_type, "value", str(message_type))
        platform = event.get_platform_name()
        self.store.bind_session(umo, event.get_sender_name() or "")
        self.store.update_scheduler_state(
            platform=platform,
            msg_type=type_name,
            session_id=event.session_id,
            self_id=event.get_self_id(),
            sender_id=event.get_sender_id(),
            sender_name=event.get_sender_name(),
            group_id=event.get_group_id() or "",
        )
        note = ""
        if platform != "aiocqhttp":
            note = (
                f"\n⚠ 当前平台是 {platform}，AstrBot 的事件注入目前只支持 aiocqhttp。"
                "定时返场可能无法唤醒，请改用 aiocqhttp 会话，或只用 /atl heartbeat 手动触发验证。"
            )
        return (
            f"已把返场绑定到当前会话：{umo}\n"
            + ("返场调度已开启。" if self._sched().enabled else "提醒：配置里 heartbeat_enabled 还是关闭的，开了才会自动返场。")
            + note
        )

    async def _do_trigger(self, trigger: str) -> str:
        if not self.store.bound_session():
            return "还没绑定会话。先在目标会话里执行 /atl bind。"
        if not self.store.credentials().has_key:
            return "还没有 api_key。先 /atl register <名字> 或 /atl key set <key>。"
        await self._sched().trigger_now(trigger)
        label = "每日规则同步" if trigger == "skill_update" else "返场"
        return f"已手动触发一次{label}，接下来的动作由 bot 自己决定。"
