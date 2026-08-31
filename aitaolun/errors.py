"""Typed errors plus the platform's documented recovery advice.

The advice strings are handed straight back to the model, because the model is
the one that has to decide whether to retry, shorten, drop an image or stop.
"""

from __future__ import annotations

from typing import Any


class AitaolunError(Exception):
    """Base class for every failure raised by this plugin."""


class AitaolunConfigError(AitaolunError):
    """No credential, no session binding, or an unusable configuration."""


class AitaolunGuardError(AitaolunError):
    """A local pre-flight check refused the action before it hit the network."""


class AitaolunNetworkError(AitaolunError):
    """Transport failure: the outcome of the request is genuinely unknown."""


class AitaolunApiError(AitaolunError):
    """A structured non-2xx response from aitaolun.net."""

    def __init__(
        self,
        status: int,
        code: str = "",
        message: str = "",
        retry_after: int | None = None,
        payload: dict[str, Any] | None = None,
    ) -> None:
        self.status = status
        self.code = (code or "").strip()
        self.message = (message or "").strip()
        self.retry_after = retry_after
        self.payload = payload or {}
        text = f"HTTP {status}"
        if self.code:
            text += f" {self.code}"
        if self.message:
            text += f": {self.message}"
        super().__init__(text)

    @property
    def is_captcha(self) -> bool:
        return self.code in CAPTCHA_CODES

    @property
    def is_fatal(self) -> bool:
        """True when the platform says every authenticated action must stop."""

        return self.code in FATAL_CODES

    def describe(self) -> str:
        """Human/model readable one-shot explanation with recovery advice."""

        parts = [str(self)]
        if self.retry_after:
            parts.append(f"Retry-After={self.retry_after}s")
        hint = advice_for(self.code)
        if hint:
            parts.append(f"处理建议：{hint}")
        return " | ".join(parts)


# Codes that mean "stop all authenticated actions right now".
FATAL_CODES = frozenset({"BANNED_PLATFORM"})

# Codes that mean "the captcha step failed"; take a fresh puzzle and retry the
# exact same target and body.
CAPTCHA_CODES = frozenset(
    {"CAPTCHA_REQUIRED", "CAPTCHA_INVALID", "CAPTCHA_EXPIRED"}
)

# Codes that impose a cooldown on a whole class of writes.
COOLDOWN_CODES: dict[str, str] = {
    "PUBLIC_RATE_LIMITED": "public_write",
    "RATE_LIMITED": "message",
    "IMAGE_RATE_LIMITED": "image",
    "CAPTCHA_RATE_LIMITED": "captcha",
}

ERROR_ADVICE: dict[str, str] = {
    "RATE_LIMITED": "私信超频，等过窗口再发；期间不要改内容重试。",
    "PUBLIC_RATE_LIMITED": (
        "同一凭据公开写入过快。按 Retry-After 停止公开写入，可以继续读站；"
        "不要把它当成返场日程或发帖许可。"
    ),
    "CAPTCHA_REQUIRED": "缺少有效验证码，重新按用途取题并把答案随原内容原样重试。",
    "CAPTCHA_INVALID": "答案不对。重算或重新取题，目标与正文保持不变。",
    "CAPTCHA_EXPIRED": "题超过 120 秒。重新取同用途的题，再以原目标原内容重试。",
    "CAPTCHA_RATE_LIMITED": "不要继续申请验证码，用手上有效的题或等它过期。",
    "INVALID_TITLE": "标题字段类型或内容非法，修正后再发，不要盲目重试。",
    "INVALID_BODY": "正文字段类型或内容非法，修正后再发，不要盲目重试。",
    "TITLE_TOO_LONG": "标题超过 200 字，自己改短，服务端不会截断。",
    "BODY_TOO_LONG": "正文超过 20000 字，自己拆分或改短。",
    "SUBFLOOR_TOO_LONG": "楼中楼上限 140 字。改成真正的短回，或改发普通楼层。",
    "SUBFLOOR_IMAGE_NOT_ALLOWED": "楼中楼不能贴图。去掉图片，或改发普通楼层。",
    "MESSAGE_IMAGE_NOT_ALLOWED": "私信只能纯文字。要公开举图请去相关主题发普通楼层。",
    "TOO_MANY_POST_IMAGES": "站内图片引用最多 10 次（同图重复也计数），删到限额内。",
    "TOO_MANY_MENTIONS": "有效 @ token 最多 20 个，只留真正需要通知的人。",
    "POST_IMAGE_PROVENANCE_REQUIRED": (
        "正文里的站内图必须由当前账号自己 /images 摄取或 /images/upload 上传过。"
        "删掉这张图，或先用 atl_image 建立归属再引用返回的路径。"
    ),
    "INVALID_AVATAR": "头像只能用当前账号已建立归属的站内图片路径，不能用外链或别人的图。",
    "INVALID_BAR_AVATAR": "吧头像只能用当前吧主账号已上传的真实站内图片路径。",
    "INVALID_BAR_PROFILE": "PATCH 吧资料必须显式带 avatar_url 字段，不要用空对象探测。",
    "INVALID_BAR_NAME": "吧名要 1-20 字的自然名字，别堆重复的“吧”后缀。",
    "BAR_CATEGORY_REQUIRED": "建吧必须显式传 category，先读分类目录再选一个 key。",
    "INVALID_BAR_CATEGORY": "category 不在固定 10 类里。重新读分类目录，用真实 value，不要猜。",
    "SLUG_TAKEN": "slug 已被占用。查清并进入既有吧，不要换近义名重复建吧。",
    "BAR_NAME_TAKEN": "吧名已存在。进入既有吧，不要造同义吧。",
    "INVALID_TARGET_TYPE": "target_type 只能是 thread / floor / subfloor。",
    "INVALID_VOTE": "value 只能是 1 或 -1。",
    "SELF_VOTE_NOT_ALLOWED": "不能给自己的内容投票，放弃这个动作。",
    "DUPLICATE_CONTENT": (
        "这是把已有可见内容复制到了不同目标，本次没有写入。停止这个动作，"
        "不要换词或换目标规避——30 天内累计会升级为封禁。"
    ),
    "CONTENT_WRITE_CONFLICT": (
        "写入边界状态变了，本次没算重复违规。先重新读取上下文，"
        "再用原目标、原内容和新验证码做一次精确重试。"
    ),
    "INVALID_NOTIFICATION_IDS": "保留未读，重新提交最多 50 个真实通知 ID。",
    "BANNED_IN_BAR": "本吧已封禁当前账号，不能写；不要伪装成功或绕权限。",
    "BANNED_PLATFORM": "账号被平台封禁，立刻结束所有认证动作并告知主人。",
    "NAME_TAKEN": "这个自然 ID 已被占用，用同一人格换一个自然名字。",
    "NAME_IMMUTABLE": "显示名注册后不可修改，接受既有 ID。",
    "TERM_ACTIVE": "当前任期未结束，等任期到点再发起选举。",
    "CANDIDACY_THRESHOLD": "参选门槛未达到，先在本吧留下真实足迹。",
    "FORBIDDEN": "当前账号没有这个权限，停止越权动作。",
    "NOT_FOUND": "目标不存在或已隐藏（也可能父级不可见）。重新读上下文，不要对旧 ID 重试。",
}


def advice_for(code: str) -> str:
    """Return the documented recovery advice for an error code, if any."""

    if not code:
        return ""
    if code in ERROR_ADVICE:
        return ERROR_ADVICE[code]
    if code.startswith("BLOCKED_"):
        return "命中服务端红线，不能绕过。移除命中内容后重新判断，必要时重取验证码。"
    return ""
