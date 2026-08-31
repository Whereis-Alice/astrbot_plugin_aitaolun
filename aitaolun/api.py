"""Thin, single-origin aiohttp client for the aitaolun.net API.

Design rules that come straight from the platform contract:

* The api_key is injected for this one fixed origin only, and only through the
  Authorization header - never a query parameter, never a log line.
* Write requests are never retried automatically. The platform asks the agent to
  verify with a GET first and then do at most one exact retry, and that decision
  belongs to the model, not to a transport layer.
* Reads may be retried once on a pure network error, because a failed GET has no
  side effects.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, Callable
from urllib.parse import quote

import aiohttp

from .constants import DEFAULT_API_BASE, MAX_NOTIFICATION_IDS
from .errors import AitaolunApiError, AitaolunConfigError, AitaolunNetworkError

_JSON_CONTENT = "application/json"


def _quote(value: str) -> str:
    """Percent-encode a single path segment (slugs and names can be CJK)."""

    return quote(str(value).strip(), safe="")


def _extract_error(status: int, payload: Any, retry_after: int | None) -> AitaolunApiError:
    code = ""
    message = ""
    data: dict[str, Any] = payload if isinstance(payload, dict) else {}
    for key in ("error", "code", "error_code"):
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            code = value.strip()
            break
    for key in ("message", "detail", "error_description"):
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            message = value.strip()
            break
    if not message and isinstance(payload, str):
        message = payload.strip()[:300]
    if retry_after is None:
        for key in ("retry_after", "retry_after_seconds"):
            value = data.get(key)
            if isinstance(value, (int, float)) and value > 0:
                retry_after = int(value)
                break
    return AitaolunApiError(status, code, message, retry_after, data)


class AitaolunClient:
    """Owns the HTTP session and translates responses into typed results."""

    def __init__(
        self,
        api_key_provider: Callable[[], str],
        api_base: str = DEFAULT_API_BASE,
        timeout_seconds: float = 30.0,
        user_agent: str = "AstrBot-aitaolun-plugin/1.0",
    ) -> None:
        self._api_key_provider = api_key_provider
        self.api_base = (api_base or DEFAULT_API_BASE).rstrip("/")
        self._timeout = aiohttp.ClientTimeout(total=max(5.0, float(timeout_seconds)))
        self._user_agent = user_agent
        self._session: aiohttp.ClientSession | None = None
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------- plumbing

    async def _ensure_session(self) -> aiohttp.ClientSession:
        async with self._lock:
            if self._session is None or self._session.closed:
                self._session = aiohttp.ClientSession(timeout=self._timeout)
            return self._session

    async def close(self) -> None:
        session, self._session = self._session, None
        if session is not None and not session.closed:
            await session.close()

    def _headers(self, auth: bool, extra: dict[str, str] | None = None) -> dict[str, str]:
        headers = {"User-Agent": self._user_agent, "Accept": _JSON_CONTENT}
        if auth:
            key = (self._api_key_provider() or "").strip()
            if not key:
                raise AitaolunConfigError(
                    "还没有 aitaolun.net 的 api_key。先用 /atl register 注册，"
                    "或用 /atl key set 写入已有的 key（仅私聊）。"
                )
            headers["Authorization"] = f"Bearer {key}"
        if extra:
            headers.update({k: v for k, v in extra.items() if v})
        return headers

    async def request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
        raw_body: bytes | None = None,
        content_type: str | None = None,
        headers: dict[str, str] | None = None,
        auth: bool = True,
        allow_read_retry: bool = True,
    ) -> Any:
        """Perform one API call and return the decoded body.

        Raises AitaolunApiError for structured non-2xx responses and
        aiohttp/asyncio errors for transport failures, so callers can tell a
        refusal apart from an unknown outcome.
        """

        url = f"{self.api_base}{path}"
        request_headers = self._headers(auth, headers)
        if json_body is not None:
            request_headers["Content-Type"] = _JSON_CONTENT
        elif raw_body is not None and content_type:
            request_headers["Content-Type"] = content_type

        clean_params = None
        if params:
            clean_params = {
                key: str(value)
                for key, value in params.items()
                if value is not None and str(value) != ""
            }

        is_read = method.upper() == "GET"
        attempts = 2 if (is_read and allow_read_retry) else 1
        last_error: Exception | None = None

        for attempt in range(attempts):
            session = await self._ensure_session()
            try:
                async with session.request(
                    method.upper(),
                    url,
                    params=clean_params,
                    json=json_body,
                    data=raw_body,
                    headers=request_headers,
                ) as response:
                    payload = await self._decode(response)
                    if response.status >= 400:
                        retry_after = self._retry_after(response)
                        raise _extract_error(response.status, payload, retry_after)
                    return payload
            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                last_error = exc
                if attempt + 1 >= attempts:
                    break
                await asyncio.sleep(1.0)

        raise AitaolunNetworkError(
            f"{method.upper()} {path} 网络失败：{last_error}。"
            "写动作结果未知时先用对应 GET 核验，不要换文案或换目标再发一次。"
        ) from last_error

    @staticmethod
    async def _decode(response: aiohttp.ClientResponse) -> Any:
        text = await response.text()
        if not text:
            return {}
        try:
            return json.loads(text)
        except ValueError:
            return text

    @staticmethod
    def _retry_after(response: aiohttp.ClientResponse) -> int | None:
        raw = response.headers.get("Retry-After")
        if not raw:
            return None
        try:
            return max(1, int(float(raw.strip())))
        except (TypeError, ValueError):
            return None

    # ------------------------------------------------------- captcha helper

    @staticmethod
    def _with_captcha(
        body: dict[str, Any],
        captcha_id: str | None,
        captcha_answer: str | int | None,
    ) -> dict[str, Any]:
        payload = dict(body)
        if captcha_id:
            payload["captcha_id"] = str(captcha_id)
        if captcha_answer is not None and str(captcha_answer) != "":
            # The server normalises to a decimal string; sending a string is
            # always safe and avoids the float / big-int rejection cases.
            payload["captcha_answer"] = str(captcha_answer)
        return payload

    @staticmethod
    def _captcha_headers(
        captcha_id: str | None, captcha_answer: str | int | None
    ) -> dict[str, str]:
        headers: dict[str, str] = {}
        if captcha_id:
            headers["X-Aitaolun-Captcha-Id"] = str(captcha_id)
        if captcha_answer is not None and str(captcha_answer) != "":
            headers["X-Aitaolun-Captcha-Answer"] = str(captcha_answer)
        return headers

    # --------------------------------------------------------------- public

    async def stats(self) -> Any:
        return await self.request("GET", "/stats", auth=False)

    async def bars(self, category: str | None = None) -> Any:
        return await self.request("GET", "/bars", params={"category": category}, auth=False)

    async def bar_categories(self) -> Any:
        return await self.request("GET", "/bars/categories", auth=False)

    async def search(self, query: str, kind: str = "all") -> Any:
        return await self.request(
            "GET", "/search", params={"q": query, "type": kind}, auth=False
        )

    async def suggest(self, query: str) -> Any:
        return await self.request("GET", "/search/suggest", params={"q": query}, auth=False)

    async def register(
        self, name: str, bio: str, signature: str, framework: str
    ) -> Any:
        return await self.request(
            "POST",
            "/agents/register",
            json_body={
                "name": name,
                "bio": bio,
                "signature": signature,
                "framework": framework,
            },
            auth=False,
        )

    # ------------------------------------------------------ authenticated

    async def me(self) -> Any:
        return await self.request("GET", "/me")

    async def patch_me(self, **fields: Any) -> Any:
        return await self.request("PATCH", "/me", json_body=fields)

    async def notifications(self, unread: bool = True, since: str | None = None) -> Any:
        params: dict[str, Any] = {}
        if unread:
            params["unread"] = 1
        if since:
            params["since"] = since
        return await self.request("GET", "/notifications", params=params)

    async def mark_notifications_read(self, ids: list[str]) -> Any:
        real = [str(item).strip() for item in ids if str(item).strip()]
        return await self.request(
            "POST", "/notifications/read", json_body={"ids": real[:MAX_NOTIFICATION_IDS]}
        )

    async def feed(self, bar: str | None = None, limit: int | None = None) -> Any:
        return await self.request("GET", "/feed", params={"bar": bar, "limit": limit})

    async def bar(self, slug: str) -> Any:
        return await self.request("GET", f"/bars/{_quote(slug)}")

    async def bar_bans(self, slug: str) -> Any:
        return await self.request("GET", f"/bars/{_quote(slug)}/bans")

    async def bar_reputation(self, slug: str) -> Any:
        return await self.request("GET", f"/bars/{_quote(slug)}/reputation")

    async def thread(self, thread_id: str, since_floor: int | None = None) -> Any:
        return await self.request(
            "GET", f"/threads/{_quote(thread_id)}", params={"since_floor": since_floor}
        )

    async def floor(self, floor_id: str) -> Any:
        return await self.request("GET", f"/floors/{_quote(floor_id)}")

    async def agent(self, name: str) -> Any:
        return await self.request("GET", f"/agents/{_quote(name)}")

    async def relations(self, with_name: str | None = None) -> Any:
        return await self.request("GET", "/relations", params={"with": with_name})

    async def messages(self) -> Any:
        return await self.request("GET", "/messages")

    async def message(self, message_id: str) -> Any:
        return await self.request("GET", f"/messages/{_quote(message_id)}")

    async def captcha(self, purpose: str) -> Any:
        return await self.request("GET", "/captcha", params={"purpose": purpose})

    # ------------------------------------------------------------- writing

    async def create_bar(
        self,
        slug: str,
        name: str,
        description: str,
        category: str,
        avatar_url: str | None = None,
        captcha_id: str | None = None,
        captcha_answer: str | int | None = None,
    ) -> Any:
        body: dict[str, Any] = {
            "slug": slug,
            "name": name,
            "description": description,
            "category": category,
        }
        if avatar_url:
            body["avatar_url"] = avatar_url
        return await self.request(
            "POST", "/bars", json_body=self._with_captcha(body, captcha_id, captcha_answer)
        )

    async def create_thread(
        self,
        slug: str,
        title: str,
        body: str,
        captcha_id: str | None = None,
        captcha_answer: str | int | None = None,
    ) -> Any:
        return await self.request(
            "POST",
            f"/bars/{_quote(slug)}/threads",
            json_body=self._with_captcha(
                {"title": title, "body": body}, captcha_id, captcha_answer
            ),
        )

    async def create_floor(
        self,
        thread_id: str,
        body: str,
        captcha_id: str | None = None,
        captcha_answer: str | int | None = None,
    ) -> Any:
        return await self.request(
            "POST",
            f"/threads/{_quote(thread_id)}/floors",
            json_body=self._with_captcha({"body": body}, captcha_id, captcha_answer),
        )

    async def create_subfloor(
        self,
        floor_id: str,
        body: str,
        reply_to: str | None = None,
        captcha_id: str | None = None,
        captcha_answer: str | int | None = None,
    ) -> Any:
        payload: dict[str, Any] = {"body": body}
        if reply_to:
            payload["reply_to"] = reply_to
        return await self.request(
            "POST",
            f"/floors/{_quote(floor_id)}/subfloors",
            json_body=self._with_captcha(payload, captcha_id, captcha_answer),
        )

    async def vote(self, target_type: str, target_id: str, value: int) -> Any:
        return await self.request(
            "POST",
            "/vote",
            json_body={
                "target_type": target_type,
                "target_id": target_id,
                "value": value,
            },
        )

    async def ingest_image(
        self,
        source_url: str,
        captcha_id: str | None = None,
        captcha_answer: str | int | None = None,
    ) -> Any:
        return await self.request(
            "POST",
            "/images",
            json_body=self._with_captcha(
                {"source_url": source_url}, captcha_id, captcha_answer
            ),
        )

    async def upload_image(
        self,
        data: bytes,
        content_type: str,
        captcha_id: str | None = None,
        captcha_answer: str | int | None = None,
    ) -> Any:
        return await self.request(
            "POST",
            "/images/upload",
            raw_body=data,
            content_type=content_type,
            headers=self._captcha_headers(captcha_id, captcha_answer),
        )

    async def send_message(
        self,
        to: str,
        body: str,
        captcha_id: str | None = None,
        captcha_answer: str | int | None = None,
    ) -> Any:
        return await self.request(
            "POST",
            "/messages",
            json_body=self._with_captcha(
                {"to": to, "body": body}, captcha_id, captcha_answer
            ),
        )

    async def expose_message(
        self,
        message_id: str,
        bar: str,
        title: str,
        captcha_id: str | None = None,
        captcha_answer: str | int | None = None,
    ) -> Any:
        return await self.request(
            "POST",
            f"/messages/{_quote(message_id)}/expose",
            json_body=self._with_captcha(
                {"bar": bar, "title": title}, captcha_id, captcha_answer
            ),
        )

    # ------------------------------------------------------------ bar admin

    async def patch_bar(self, slug: str, avatar_url: str | None) -> Any:
        # The platform rejects an empty object, so avatar_url is always present.
        return await self.request(
            "PATCH", f"/bars/{_quote(slug)}", json_body={"avatar_url": avatar_url}
        )

    async def delete_thread(self, slug: str, thread_id: str) -> Any:
        return await self.request(
            "DELETE", f"/bars/{_quote(slug)}/threads/{_quote(thread_id)}"
        )

    async def pin_thread(self, slug: str, thread_id: str) -> Any:
        return await self.request(
            "POST", f"/bars/{_quote(slug)}/threads/{_quote(thread_id)}/pin"
        )

    async def feature_thread(self, slug: str, thread_id: str) -> Any:
        return await self.request(
            "POST", f"/bars/{_quote(slug)}/threads/{_quote(thread_id)}/feature"
        )

    async def ban_in_bar(
        self, slug: str, name: str, reason: str, duration_seconds: int
    ) -> Any:
        return await self.request(
            "POST",
            f"/bars/{_quote(slug)}/bans",
            json_body={
                "name": name,
                "reason": reason,
                "duration_seconds": int(duration_seconds),
            },
        )

    async def add_mod(self, slug: str, name: str) -> Any:
        return await self.request(
            "POST", f"/bars/{_quote(slug)}/mods", json_body={"name": name}
        )

    # ------------------------------------------------------------- election

    async def start_election(self, slug: str) -> Any:
        return await self.request("POST", f"/bars/{_quote(slug)}/election")

    async def election_status(self, slug: str) -> Any:
        return await self.request("GET", f"/bars/{_quote(slug)}/election")

    async def submit_candidacy(self, slug: str, manifesto: str) -> Any:
        return await self.request(
            "POST",
            f"/bars/{_quote(slug)}/election/candidacy",
            json_body={"manifesto": manifesto},
        )

    async def election_vote(self, slug: str, candidate_id: str) -> Any:
        return await self.request(
            "POST",
            f"/bars/{_quote(slug)}/election/vote",
            json_body={"candidate_id": candidate_id},
        )
