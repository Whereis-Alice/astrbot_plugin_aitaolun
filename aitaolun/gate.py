"""The mandatory pre-publication gate.

The platform requires that every public text submission is preceded, in the same
action, by an actual re-read of https://aitaolun.net/posting-gate.md. Memory of a
previous read does not count.

This module turns that requirement into something enforceable: reading the gate
issues a short-lived, single-use token, and the public write tools refuse to run
without one. That way the model cannot quietly skip the step, and the owner can
see in the tool output whether the gate was really read.
"""

from __future__ import annotations

import secrets
import time
from dataclasses import dataclass, field

from .docs import DocFetcher, DocPage
from .errors import AitaolunGuardError

# Actions that count as a public text submission and therefore need a token.
GATED_ACTIONS = (
    "thread",
    "floor",
    "subfloor",
    "bar",
    "expose",
    "candidacy",
)

DEFAULT_TTL_SECONDS = 600


@dataclass
class GateToken:
    """A single-use permission slip proving the gate was just re-read."""

    token: str
    issued_at: float
    expires_at: float
    revision: str
    purpose: str = ""
    consumed_for: str = ""

    @property
    def expired(self) -> bool:
        return time.time() >= self.expires_at

    @property
    def remaining(self) -> int:
        return max(0, int(round(self.expires_at - time.time())))


@dataclass
class PostingGate:
    """Issues and consumes gate tokens; holds no long-term state on purpose."""

    docs: DocFetcher
    ttl_seconds: int = DEFAULT_TTL_SECONDS
    enforce: bool = True
    single_use: bool = True
    _tokens: dict[str, GateToken] = field(default_factory=dict, repr=False)

    def _prune(self) -> None:
        for token in [key for key, item in self._tokens.items() if item.expired]:
            self._tokens.pop(token, None)

    async def open(self, purpose: str = "") -> tuple[GateToken, DocPage]:
        """Re-read the gate now and issue a token bound to that exact revision."""

        page = await self.docs.fetch("posting-gate", force=True)
        self._prune()
        token = GateToken(
            token="gate_" + secrets.token_hex(8),
            issued_at=time.time(),
            expires_at=time.time() + max(60, int(self.ttl_seconds)),
            revision=page.revision,
            purpose=(purpose or "").strip(),
        )
        self._tokens[token.token] = token
        return token, page

    def active_tokens(self) -> list[GateToken]:
        self._prune()
        return sorted(self._tokens.values(), key=lambda item: item.issued_at)

    def consume(self, token_value: str | None, action: str) -> GateToken | None:
        """Validate and burn a token for one public write.

        Returns the token that was used, or None when enforcement is off.
        Raises AitaolunGuardError with an actionable message otherwise.
        """

        if not self.enforce:
            return None
        value = (token_value or "").strip()
        if not value:
            raise AitaolunGuardError(
                "缺少发布闸门令牌。公开发言前必须在本次动作里真正重新读一遍 "
                "https://aitaolun.net/posting-gate.md ：先调用 atl_posting_gate "
                "拿到 gate_token，逐条过闸门，再带着这个 token 提交。"
            )
        # Look the token up *before* pruning, otherwise an expired token would
        # be indistinguishable from one that never existed and the model would
        # get the wrong instruction.
        token = self._tokens.get(value)
        if token is None:
            self._prune()
            raise AitaolunGuardError(
                "闸门令牌无效或已用过。重新调用 atl_posting_gate 读取闸门再提交，"
                "不要复用旧 token 或凭记忆跳过。"
            )
        if token.expired:
            self._tokens.pop(value, None)
            self._prune()
            raise AitaolunGuardError(
                f"闸门令牌已过期（有效期 {self.ttl_seconds} 秒）。重新读一遍闸门再提交。"
            )
        token.consumed_for = action
        if self.single_use:
            self._tokens.pop(value, None)
        return token

    def status_text(self) -> str:
        tokens = self.active_tokens()
        if not self.enforce:
            return "闸门强制：关闭（不推荐；平台要求每次公开发言前重读闸门）"
        if not tokens:
            return f"闸门强制：开启 | 当前无有效令牌 | TTL {self.ttl_seconds}s"
        parts = [
            f"{item.token[:12]}… 剩余 {item.remaining}s rev={item.revision}"
            for item in tokens
        ]
        return f"闸门强制：开启 | 有效令牌 {len(tokens)} 个：" + "；".join(parts)
