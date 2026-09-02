"""End-to-end guard behaviour with a fake HTTP client - no network at all.

These are the tests that matter most: every case here is something the platform
punishes (wasted captcha, duplicate content, writing while rate-limited or
banned, unattributed images), and the point is that it never leaves this
process.
"""

import asyncio
import time

from aitaolun.errors import (
    AitaolunApiError,
    AitaolunConfigError,
    AitaolunGuardError,
)
from aitaolun.docs import DocPage, revision_of
from aitaolun.gate import PostingGate
from aitaolun.guard import fingerprint
from aitaolun.service import CaptchaPending, AitaolunService
from aitaolun.state import StateStore

IMG = "/img/" + "a" * 24 + ".webp"
TID = "b" * 24


def run(coro):
    return asyncio.run(coro)


class FakeDocs:
    def __init__(self):
        self.fetches = 0

    async def fetch(self, name, force=True):
        self.fetches += 1
        text = "闸门：说人话，带刺，不要助手腔。"
        return DocPage(
            name=name,
            url="https://aitaolun.net/%s.md" % name,
            text=text,
            fetched_at=time.time(),
            revision=revision_of(text),
        )


class FakeClient:
    """Records every call and replays scripted results or errors."""

    def __init__(self, **results):
        self.calls = []
        self.results = results
        self.captcha_question = "12 + 5 = ?"
        self.captcha_id = "cap1"
        self.profile = {
            "name": "测试机",
            "claimed": True,
            "bio": "旧简介",
            "signature": "",
            "framework": "AstrBot",
        }

    def _record(self, name, args):
        self.calls.append((name, args))
        outcome = self.results.get(name)
        if callable(outcome):
            return outcome(self, args)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome if outcome is not None else {"id": "c" * 24}

    async def stats(self):
        return self._record("stats", {})

    async def feed(self, bar=None, limit=None):
        return self._record("feed", {"bar": bar, "limit": limit})

    async def thread(self, thread_id, since_floor=None):
        return self._record("thread", {"thread_id": thread_id, "since_floor": since_floor})

    async def floor(self, floor_id):
        return self._record("floor", {"floor_id": floor_id})

    async def captcha(self, purpose):
        self.calls.append(("captcha", {"purpose": purpose}))
        error = self.results.get("captcha")
        if isinstance(error, Exception):
            raise error
        return {"captcha_id": self.captcha_id, "question": self.captcha_question}

    async def create_thread(self, slug, title, body, captcha_id=None, answer=None):
        return self._record(
            "create_thread",
            {
                "slug": slug,
                "title": title,
                "body": body,
                "captcha_id": captcha_id,
                "answer": answer,
            },
        )

    async def create_floor(self, thread_id, body, captcha_id=None, answer=None):
        return self._record(
            "create_floor",
            {"thread_id": thread_id, "body": body, "captcha_id": captcha_id, "answer": answer},
        )

    async def create_subfloor(
        self, floor_id, body, reply_to=None, captcha_id=None, answer=None
    ):
        return self._record(
            "create_subfloor",
            {"floor_id": floor_id, "body": body, "reply_to": reply_to},
        )

    async def vote(self, target_type, target_id, value):
        return self._record(
            "vote", {"target_type": target_type, "target_id": target_id, "value": value}
        )

    async def ingest_image(self, source_url, captcha_id=None, answer=None):
        return self._record("ingest_image", {"source_url": source_url})

    async def send_message(self, to, body, captcha_id=None, answer=None):
        return self._record("send_message", {"to": to, "body": body})

    async def upload_image(self, payload, content_type, captcha_id=None, answer=None):
        return self._record(
            "upload_image", {"bytes": len(payload), "content_type": content_type}
        )

    async def me(self):
        self.calls.append(("me", {}))
        outcome = self.results.get("me")
        if isinstance(outcome, Exception):
            raise outcome
        if callable(outcome):
            return outcome(self, {})
        return outcome if outcome is not None else dict(self.profile)

    async def patch_me(self, **fields):
        self.calls.append(("patch_me", dict(fields)))
        outcome = self.results.get("patch_me")
        if isinstance(outcome, Exception):
            raise outcome
        # 把改动写回假资料，好让调用方复核时看到新值
        for key, value in fields.items():
            self.profile[key] = "" if value is None else value
        return dict(self.profile)

    def count(self, name):
        return sum(1 for item in self.calls if item[0] == name)


def build(client, *, enforce=True, with_key=True, tmp_path=None, options=None):
    store = StateStore(data_dir=tmp_path)
    if with_key:
        store.set_api_key("atl_" + "k" * 40, "测试机")
    docs = FakeDocs()
    gate = PostingGate(docs=docs, enforce=enforce)
    service = AitaolunService(
        client=client,
        store=store,
        gate=gate,
        docs=docs,
        options=dict(options or {}),
    )
    return service, store, gate


def fresh_token(gate):
    token, _ = run(gate.open("test"))
    return token.token


def expect_guard(callable_, *, contains=""):
    try:
        run(callable_())
    except AitaolunGuardError as error:
        if contains:
            assert contains in str(error), str(error)
        return str(error)
    raise AssertionError("expected a local guard refusal")


# ------------------------------------------------------------------ pre-flight


def test_reads_work_without_a_credential(tmp_path):
    client = FakeClient(stats={"agents": 3, "threads": 4})
    service, _, _ = build(client, with_key=False, tmp_path=tmp_path)
    text = run(service.stats())
    assert text
    assert client.count("stats") == 1


def test_writes_without_a_credential_fail_fast(tmp_path):
    client = FakeClient()
    service, _, gate = build(client, with_key=False, tmp_path=tmp_path)
    try:
        run(service.create_thread("shuiba", "标题", "正文", fresh_token(gate)))
    except AitaolunConfigError as error:
        assert "api_key" in str(error)
    else:
        raise AssertionError("a missing api_key must stop the write")
    assert client.calls == []


# --------------------------------------------------------------- content guard


def test_oversized_body_never_reaches_the_network_or_burns_the_gate(tmp_path):
    client = FakeClient()
    service, _, gate = build(client, tmp_path=tmp_path)
    token = fresh_token(gate)

    expect_guard(
        lambda: service.create_thread("shuiba", "标题", "x" * 20001, token),
        contains="本地校验未通过",
    )
    assert client.calls == []
    # The token is still unused, so the model does not have to re-read the gate.
    assert [item.token for item in gate.active_tokens()] == [token]


def test_subfloor_rules_are_enforced_locally(tmp_path):
    client = FakeClient()
    service, _, gate = build(client, tmp_path=tmp_path)

    expect_guard(
        lambda: service.reply("subfloor", TID, "x" * 141, gate_token=fresh_token(gate))
    )
    expect_guard(
        lambda: service.reply(
            "subfloor", TID, "看图 ![x](%s)" % IMG, gate_token=fresh_token(gate)
        )
    )
    expect_guard(lambda: service.reply("floor", "not-an-id", "正文"))
    assert client.calls == []


def test_unattributed_in_site_image_is_refused(tmp_path):
    client = FakeClient()
    service, store, gate = build(client, tmp_path=tmp_path)
    body = "配图 ![x](%s)" % IMG

    expect_guard(lambda: service.create_thread("shuiba", "标题", body, fresh_token(gate)))
    assert client.calls == []

    store.record_image(IMG, "https://example.com/a.png")
    run(service.create_thread("shuiba", "标题", body, fresh_token(gate)))
    assert client.count("create_thread") == 1


def test_private_messages_cannot_carry_images(tmp_path):
    client = FakeClient()
    service, store, gate = build(client, tmp_path=tmp_path)
    store.record_image(IMG, "own")
    expect_guard(
        lambda: service.messages(action="send", to="someone", body="![x](%s)" % IMG)
    )
    assert client.calls == []


# ------------------------------------------------------------------ gate


def test_public_write_without_a_gate_token_is_refused(tmp_path):
    client = FakeClient()
    service, _, _ = build(client, tmp_path=tmp_path)
    expect_guard(
        lambda: service.create_thread("shuiba", "标题", "正文"),
        contains="posting-gate.md",
    )
    assert client.calls == []


def test_gate_token_is_burned_by_one_successful_write(tmp_path):
    client = FakeClient()
    service, _, gate = build(client, tmp_path=tmp_path)
    token = fresh_token(gate)
    run(service.create_thread("shuiba", "标题", "第一帖", token))
    assert gate.active_tokens() == []
    expect_guard(lambda: service.create_thread("shuiba", "标题", "第二帖", token))
    assert client.count("create_thread") == 1


def test_gate_can_be_disabled_for_local_testing(tmp_path):
    client = FakeClient()
    service, _, _ = build(client, enforce=False, tmp_path=tmp_path)
    text = run(service.create_thread("shuiba", "标题", "正文"))
    assert "闸门强制已关闭" in text


# -------------------------------------------------------------- duplicates


def test_same_content_aimed_at_a_new_target_is_refused_locally(tmp_path):
    client = FakeClient()
    service, store, gate = build(client, tmp_path=tmp_path)
    store.record_write("thread", "abar", fingerprint("标题", "同一段话"), "id1")

    expect_guard(
        lambda: service.create_thread("bbar", "标题", "同一段话", fresh_token(gate)),
        contains="DUPLICATE_CONTENT",
    )
    assert client.calls == []


def test_exact_retry_on_the_same_target_is_allowed(tmp_path):
    client = FakeClient(create_thread={"id": "d" * 24, "already_exists": True})
    service, store, gate = build(client, tmp_path=tmp_path)
    store.record_write("thread", "abar", fingerprint("标题", "同一段话"), "id1")

    text = run(service.create_thread("abar", "标题", "同一段话", fresh_token(gate)))
    assert client.count("create_thread") == 1
    assert "already_exists" in text or "已存在" in text


# ----------------------------------------------------------------- captcha


def test_428_is_solved_locally_and_retried_byte_identically(tmp_path):
    def create_thread(client, args):
        if not args["captcha_id"]:
            raise AitaolunApiError(428, "CAPTCHA_REQUIRED", "captcha required")
        assert args["answer"] == "17"
        return {"thread_id": "e" * 24}

    client = FakeClient(create_thread=create_thread)
    service, _, gate = build(client, tmp_path=tmp_path)
    body = "带刺的正文\n第二行"
    run(service.create_thread("shuiba", "标题", body, fresh_token(gate)))

    attempts = [args for name, args in client.calls if name == "create_thread"]
    assert len(attempts) == 2
    # Same target, same bytes; only the captcha fields changed.
    assert attempts[0]["body"] == attempts[1]["body"] == body
    assert attempts[0]["slug"] == attempts[1]["slug"]
    assert attempts[1]["captcha_id"] == "cap1"
    assert client.count("captcha") == 1


def test_unsolvable_captcha_is_handed_back_to_the_model(tmp_path):
    client = FakeClient(
        create_thread=AitaolunApiError(428, "CAPTCHA_REQUIRED", "captcha required")
    )
    client.captcha_question = "下面哪个词是名词：苹果、跑、快"
    service, _, gate = build(client, tmp_path=tmp_path)

    try:
        run(service.create_thread("shuiba", "标题", "正文", fresh_token(gate)))
    except CaptchaPending as error:
        text = str(error)
        assert "cap1" in text
        assert client.captcha_question in text
        assert "一个字都不要改" in text
    else:
        raise AssertionError("an unsolvable captcha must come back to the model")


def test_a_wrong_supplied_answer_does_not_loop(tmp_path):
    client = FakeClient(
        create_thread=AitaolunApiError(400, "CAPTCHA_INVALID", "wrong answer")
    )
    service, _, gate = build(client, tmp_path=tmp_path)
    expect_guard(
        lambda: service.create_thread(
            "shuiba", "标题", "正文", fresh_token(gate), captcha_id="cap1", captcha_answer=99
        ),
        contains="拿一道新题",
    )
    # Exactly one attempt: no silent retry storm.
    assert client.count("create_thread") == 1


# --------------------------------------------------------------- rate limits


def test_public_rate_limit_becomes_a_local_cooldown(tmp_path):
    client = FakeClient(
        create_thread=AitaolunApiError(
            429, "PUBLIC_RATE_LIMITED", "too fast", retry_after=90
        )
    )
    service, store, gate = build(client, tmp_path=tmp_path)

    try:
        run(service.create_thread("shuiba", "标题", "正文", fresh_token(gate)))
    except AitaolunApiError as error:
        assert error.code == "PUBLIC_RATE_LIMITED"
    else:
        raise AssertionError("the server error must surface")

    cooldown = store.cooldown("public_write")
    assert cooldown is not None and 80 <= cooldown.remaining <= 90

    expect_guard(
        lambda: service.create_thread("shuiba", "别的标题", "别的正文", fresh_token(gate)),
        contains="本地冷却",
    )
    assert client.count("create_thread") == 1
    # Reading is still allowed while writes are cooling down.
    run(service.stats())


def test_message_and_image_cooldowns_are_tracked_separately(tmp_path):
    client = FakeClient(
        send_message=AitaolunApiError(429, "RATE_LIMITED", "slow"),
    )
    service, store, gate = build(client, tmp_path=tmp_path)
    try:
        run(service.messages(action="send", to="someone", body="你好"))
    except AitaolunApiError:
        pass
    assert store.cooldown("message") is not None
    # A public write is unaffected by a private-message cooldown.
    run(service.create_thread("shuiba", "标题", "正文", fresh_token(gate)))


# ---------------------------------------------------------------- ban latch


def test_platform_ban_latches_and_stops_everything_authenticated(tmp_path):
    client = FakeClient(
        create_thread=AitaolunApiError(403, "BANNED_PLATFORM", "永久封禁")
    )
    service, store, gate = build(client, tmp_path=tmp_path)

    try:
        run(service.create_thread("shuiba", "标题", "正文", fresh_token(gate)))
    except AitaolunApiError as error:
        assert error.is_fatal
    else:
        raise AssertionError("the ban must surface")

    assert store.platform_ban() is not None
    before = len(client.calls)
    expect_guard(
        lambda: service.create_thread("shuiba", "别的", "别的正文", fresh_token(gate)),
        contains="本地硬停",
    )
    expect_guard(lambda: service.vote("thread", TID, 1))
    expect_guard(lambda: service.messages(action="inbox"))
    assert len(client.calls) == before


# --------------------------------------------------------------------- misc


def test_vote_argument_validation(tmp_path):
    client = FakeClient()
    service, _, _ = build(client, tmp_path=tmp_path)
    expect_guard(lambda: service.vote("bar", TID, 1))
    expect_guard(lambda: service.vote("thread", "nope", 1))
    expect_guard(lambda: service.vote("thread", TID, 0))
    expect_guard(lambda: service.vote("thread", TID, "上"))
    assert client.calls == []
    run(service.vote("thread", TID, -1))
    assert client.count("vote") == 1


def test_image_ingest_records_provenance(tmp_path):
    client = FakeClient(ingest_image={"path": IMG})
    service, store, _ = build(client, tmp_path=tmp_path)
    text = run(service.image("ingest", source_url="https://example.com/a.png"))
    assert IMG in text
    assert store.owns_image(IMG)

    expect_guard(lambda: service.image("ingest", source_url="ftp://nope/a.png"))
    expect_guard(lambda: service.image("upload", file_path="不存在的文件.png"))


def test_run_history_is_recorded(tmp_path):
    client = FakeClient()
    service, store, _ = build(client, tmp_path=tmp_path)
    service.record_run("heartbeat", "ok", "试跑", "session-1")
    runs = store.runs()
    assert runs[0].trigger == "heartbeat"
    assert runs[0].detail == "试跑"


# ------------------------------------------------------------ 改自己的公开资料


def test_profile_update_without_fields_just_shows_the_current_profile(tmp_path):
    client = FakeClient()
    service, _, _ = build(client, tmp_path=tmp_path)
    text = run(service.profile_update())
    assert "本账号资料" in text
    assert "名字（不可修改）" in text
    assert "什么都没提交" in text
    assert "≤500 字" in text and "≤100 字" in text
    assert client.count("patch_me") == 0


def test_profile_update_enforces_the_length_limits_locally(tmp_path):
    client = FakeClient()
    service, _, _ = build(client, tmp_path=tmp_path)
    expect_guard(lambda: service.profile_update(bio="字" * 501), contains="超过平台上限 500")
    expect_guard(
        lambda: service.profile_update(signature="字" * 101), contains="超过平台上限 100"
    )
    # 一个字都不许提交出去
    assert client.count("patch_me") == 0
    # 边界值放行
    run(service.profile_update(bio="字" * 500, signature="字" * 100))
    assert client.calls[-2][0] == "patch_me"


def test_profile_update_writes_bio_and_signature_and_rereads(tmp_path):
    client = FakeClient()
    service, store, _ = build(client, tmp_path=tmp_path)
    text = run(service.profile_update(bio="一个爱吵架的 bot", signature="嘴很碎"))
    sent = dict(client.calls[-2][1])
    assert sent == {"bio": "一个爱吵架的 bot", "signature": "嘴很碎"}
    # 提交后重新拉一次 /me 复核
    assert client.calls[-1][0] == "me"
    assert "资料已更新" in text
    assert "一个爱吵架的 bot" in text
    assert store.runs(1)[0].trigger == "profile_update"


def test_profile_update_clear_words_blank_the_fields(tmp_path):
    client = FakeClient()
    service, _, _ = build(client, tmp_path=tmp_path)
    run(service.profile_update(bio="clear", signature="清空", clear_avatar=True))
    sent = dict(client.calls[-2][1])
    assert sent == {"bio": "", "signature": "", "avatar_url": None}
    # 空字符串不算清空指令，避免 LLM 手滑就把门面擦掉
    expect_guard(lambda: service.profile_update(bio="   "), contains="明确传 clear")
    expect_guard(lambda: service.profile_update(avatar="  "), contains="clear")
    expect_guard(
        lambda: service.profile_update(avatar=IMG, clear_avatar=True), contains="二选一"
    )


def test_profile_update_accepts_an_in_site_path_and_warns_when_it_is_not_ours(tmp_path):
    client = FakeClient()
    service, store, _ = build(client, tmp_path=tmp_path)

    text = run(service.profile_update(avatar=IMG))
    assert dict(client.calls[-2][1]) == {"avatar_url": IMG}
    assert "INVALID_AVATAR" in text  # 没有归属记录 → 提醒但仍然提交
    assert client.count("upload_image") == 0

    store.record_image(IMG, "早先上传的")
    text = run(service.profile_update(avatar=IMG))
    assert "INVALID_AVATAR" not in text

    expect_guard(lambda: service.profile_update(avatar="/img/nope.webp"))
    expect_guard(lambda: service.profile_update(avatar="ftp://x/a.png"))


def test_profile_update_pulls_an_external_link_into_the_site_first(tmp_path):
    client = FakeClient(ingest_image={"path": IMG})
    service, store, _ = build(client, tmp_path=tmp_path)
    text = run(service.profile_update(avatar="https://example.com/face.png"))
    assert client.count("ingest_image") == 1
    assert dict(client.calls[-2][1]) == {"avatar_url": IMG}
    assert store.owns_image(IMG)  # 引入即登记归属
    assert "INVALID_AVATAR" not in text


def test_profile_update_uploads_a_local_file_and_reuses_site_urls(tmp_path):
    picture = tmp_path / "face.png"
    picture.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 64)
    client = FakeClient(upload_image={"path": IMG})
    service, store, _ = build(client, tmp_path=tmp_path)

    run(service.profile_update(avatar=str(picture)))
    assert client.count("upload_image") == 1
    assert store.owns_image(IMG)

    # 完整站内 URL 只是取出路径，不会再花一次图片额度
    run(service.profile_update(avatar="https://aitaolun.net" + IMG))
    assert client.count("upload_image") == 1
    assert dict(client.calls[-2][1]) == {"avatar_url": IMG}

    expect_guard(lambda: service.profile_update(avatar=str(tmp_path / "缺失.png")))
    bad = tmp_path / "face.bmp"
    bad.write_bytes(b"BM" + b"0" * 32)
    expect_guard(lambda: service.profile_update(avatar=str(bad)), contains="扩展名")


def test_profile_update_needs_a_credential_and_respects_the_ban_latch(tmp_path):
    client = FakeClient()
    service, store, _ = build(client, with_key=False, tmp_path=tmp_path)
    try:
        run(service.profile_update(bio="随便"))
    except AitaolunConfigError as error:
        assert "api_key" in str(error)
    else:
        raise AssertionError("a missing api_key must stop the write")
    assert client.calls == []

    service, store, _ = build(FakeClient(), tmp_path=tmp_path)
    store.set_platform_banned(True, "刷屏")
    expect_guard(lambda: service.profile_update(bio="随便"), contains="本地硬停")


# ------------------------------------------------------------------ self-talk


def thread_page(*floor_authors, owner="测试机", thread_id=TID):
    """A GET /threads/{id} payload with the given accounts speaking in it."""

    return {
        "thread": {
            "id": thread_id,
            "title": "标题",
            "bar": "闲聊",
            "author_name": owner,
            "body": "正文",
        },
        "floors": [
            {
                "id": "%024x" % (index + 1),
                "number": index + 1,
                "author_name": name,
                "body": "楼层正文",
            }
            for index, name in enumerate(floor_authors)
        ],
    }


def test_own_thread_nobody_answered_cannot_be_padded(tmp_path):
    client = FakeClient()
    service, store, gate = build(client, tmp_path=tmp_path)
    run(service.create_thread("闲聊", "标题", "正文", fresh_token(gate)))
    mine = "c" * 24
    assert store.own_content(mine)["kind"] == "thread"

    error = expect_guard(
        lambda: service.reply("floor", mine, "自己再补一层", gate_token=fresh_token(gate)),
        contains="自己给自己补楼",
    )
    assert "atl_read" in error
    assert client.count("create_floor") == 0


def test_a_real_answer_clears_the_self_padding_guard(tmp_path):
    client = FakeClient(thread=lambda self, args: thread_page("路人甲"))
    service, store, gate = build(client, tmp_path=tmp_path)
    run(service.create_thread("闲聊", "标题", "正文", fresh_token(gate)))
    mine = "c" * 24

    text = run(service.read("thread", mine))
    assert "测试机（你）" in text
    assert "路人甲" in text
    assert store.thread_read(mine)["self_only"] is False

    run(service.reply("floor", mine, "接着他的话说", gate_token=fresh_token(gate)))
    assert client.count("create_floor") == 1


def test_reading_own_empty_thread_warns_and_keeps_the_block(tmp_path):
    client = FakeClient(thread=lambda self, args: thread_page("测试机"))
    service, store, gate = build(client, tmp_path=tmp_path)

    text = run(service.read("thread", TID))
    assert "除你之外还没有任何账号回过" in text
    assert store.thread_read(TID)["self_only"] is True
    expect_guard(
        lambda: service.reply("floor", TID, "再来一层", gate_token=fresh_token(gate)),
        contains="除了你自己没有任何账号发言",
    )
    assert client.count("create_floor") == 0


def test_an_unreadable_payload_does_not_clear_an_earlier_observation(tmp_path):
    client = FakeClient(thread=lambda self, args: {"weird": True})
    service, store, gate = build(client, tmp_path=tmp_path)
    store.note_thread_read(TID, self_only=True)

    run(service.read("thread", TID))
    assert store.thread_read(TID)["self_only"] is True
    expect_guard(
        lambda: service.reply("floor", TID, "补一层", gate_token=fresh_token(gate)),
        contains="本地拦截",
    )


def test_someone_elses_quiet_thread_is_always_answerable(tmp_path):
    client = FakeClient(thread=lambda self, args: thread_page(owner="路人甲"))
    service, store, gate = build(client, tmp_path=tmp_path)

    run(service.read("thread", TID))
    assert store.thread_read(TID)["self_only"] is False
    run(service.reply("floor", TID, "第一个来的", gate_token=fresh_token(gate)))
    assert client.count("create_floor") == 1


def test_a_stale_observation_stops_blocking(tmp_path):
    client = FakeClient()
    service, store, gate = build(client, tmp_path=tmp_path)
    run(service.create_thread("闲聊", "标题", "正文", fresh_token(gate)))
    mine = "c" * 24

    # 半小时以上的旧观察不再算证据：模型随时可以重读，硬拦下去只会变成死结
    store.runtime()["thread_reads"][mine]["at"] = time.time() - 31 * 60
    run(service.reply("floor", mine, "过了很久再补充", gate_token=fresh_token(gate)))
    assert client.count("create_floor") == 1


def test_answering_own_subfloor_is_refused(tmp_path):
    client = FakeClient()
    service, store, gate = build(client, tmp_path=tmp_path)
    run(service.reply("subfloor", TID, "先说一句", gate_token=fresh_token(gate)))
    mine = "c" * 24
    assert store.own_content(mine)["kind"] == "subfloor"

    expect_guard(
        lambda: service.reply(
            "subfloor", TID, "自己接自己的话", reply_to=mine, gate_token=fresh_token(gate)
        ),
        contains="自己回自己",
    )
    assert client.count("create_subfloor") == 1


def test_a_subfloor_under_own_floor_answering_nobody_is_refused(tmp_path):
    client = FakeClient()
    service, store, gate = build(client, tmp_path=tmp_path)
    run(service.reply("floor", TID, "我自己的楼层", gate_token=fresh_token(gate)))
    mine = "c" * 24
    assert store.own_content(mine)["kind"] == "floor"

    # 挂在自己楼层下、又谁都不接：这就是在自己楼下自言自语
    expect_guard(
        lambda: service.reply(
            "subfloor", mine, "自己在自己楼下补一句", gate_token=fresh_token(gate)
        ),
        contains="自言自语",
    )
    assert client.count("create_subfloor") == 0


def test_answering_a_real_person_under_own_floor_stays_allowed(tmp_path):
    client = FakeClient()
    service, store, gate = build(client, tmp_path=tmp_path)
    run(service.reply("floor", TID, "我自己的楼层", gate_token=fresh_token(gate)))
    mine = "c" * 24
    someone = "d" * 24

    run(
        service.reply(
            "subfloor",
            mine,
            "接你这句",
            reply_to=someone,
            gate_token=fresh_token(gate),
        )
    )
    assert client.count("create_subfloor") == 1
    assert client.calls[-1][1]["reply_to"] == someone


def test_endless_back_and_forth_in_one_place_is_capped(tmp_path):
    client = FakeClient()
    service, store, gate = build(client, tmp_path=tmp_path, options={"same_target_write_limit": 2})

    run(service.reply("floor", TID, "第一次接他", gate_token=fresh_token(gate)))
    run(service.reply("floor", TID, "第二次接他", gate_token=fresh_token(gate)))
    error = expect_guard(
        lambda: service.reply("floor", TID, "第三次接他", gate_token=fresh_token(gate)),
        contains="到上限了",
    )
    assert "换个帖" in error
    assert client.count("create_floor") == 2

    # 上限是按目标算的，别的帖子照常能发
    run(service.reply("floor", "e" * 24, "另一个帖", gate_token=fresh_token(gate)))
    assert client.count("create_floor") == 3


def test_the_same_target_cap_counts_subfloors_by_their_floor(tmp_path):
    client = FakeClient()
    service, store, gate = build(client, tmp_path=tmp_path, options={"same_target_write_limit": 1})

    run(service.reply("subfloor", TID, "一句", gate_token=fresh_token(gate)))
    expect_guard(
        lambda: service.reply("subfloor", TID, "又一句", gate_token=fresh_token(gate)),
        contains="楼层",
    )
    # 同一个 ID 换成普通楼层是另一条通道，不共用计数
    run(service.reply("floor", TID, "改发普通楼层", gate_token=fresh_token(gate)))
    assert client.count("create_floor") == 1


def test_the_same_target_cap_rolls_over(tmp_path):
    client = FakeClient()
    service, store, gate = build(client, tmp_path=tmp_path, options={"same_target_write_limit": 1})
    run(service.reply("floor", TID, "很久以前那次", gate_token=fresh_token(gate)))

    store.runtime()["target_writes"]["floor:" + TID] = [time.time() - 25 * 3600]
    run(service.reply("floor", TID, "一天后再回来", gate_token=fresh_token(gate)))
    assert client.count("create_floor") == 2


def test_the_same_target_cap_can_be_switched_off(tmp_path):
    client = FakeClient()
    service, _, gate = build(client, tmp_path=tmp_path, options={"same_target_write_limit": 0})
    for index in range(5):
        run(service.reply("floor", TID, "第 %d 次" % index, gate_token=fresh_token(gate)))
    assert client.count("create_floor") == 5


def test_a_broken_same_target_limit_falls_back_to_the_default(tmp_path):
    client = FakeClient()
    service, _, _ = build(client, tmp_path=tmp_path, options={"same_target_write_limit": "很多"})
    assert service._same_target_limit() == 3


def test_voting_on_own_content_never_reaches_the_network(tmp_path):
    client = FakeClient()
    service, _, gate = build(client, tmp_path=tmp_path)
    run(service.create_thread("闲聊", "标题", "正文", fresh_token(gate)))
    mine = "c" * 24

    expect_guard(
        lambda: service.vote("thread", mine, 1), contains="SELF_VOTE_NOT_ALLOWED"
    )
    assert client.count("vote") == 0
    run(service.vote("thread", TID, 1))
    assert client.count("vote") == 1


def test_feed_marks_own_threads(tmp_path):
    client = FakeClient(
        feed=lambda self, args: {
            "threads": [
                {"id": TID, "title": "别人的帖", "author_name": "路人甲"},
                {"id": "c" * 24, "title": "我的帖", "author_name": "测试机"},
            ]
        }
    )
    service, _, _ = build(client, tmp_path=tmp_path)

    text = run(service.feed())
    assert "（你自己开的帖）" in text
    assert "别自己顶自己" in text


def test_floor_detail_flags_that_the_last_voice_is_mine(tmp_path):
    client = FakeClient(
        floor=lambda self, args: {
            "floor": {
                "id": TID,
                "thread_id": "e" * 24,
                "number": 3,
                "author_name": "测试机",
                "body": "我的楼层",
            }
        }
    )
    service, _, _ = build(client, tmp_path=tmp_path)

    text = run(service.read("floor", TID))
    assert "测试机（你）" in text
    assert "最后说话的还是你自己" in text
