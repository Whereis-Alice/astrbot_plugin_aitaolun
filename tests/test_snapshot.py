"""The screenshot path: HTML building, the render ladder, routing and the tool.

Nothing here touches a network, a browser or the filesystem. The two failure
modes worth guarding are covered on purpose: post content escaping into the
remote Jinja2 template, and a render failure swallowing the content instead of
degrading to text.
"""

import asyncio
import time
import types

from aitaolun import formatting as fmt
from aitaolun import snapshot as snap
from aitaolun.constants import MAX_AVATAR_LOOKUPS
from aitaolun.docs import DocPage, revision_of
from aitaolun.errors import (
    AitaolunApiError,
    AitaolunConfigError,
    AitaolunGuardError,
)
from aitaolun.gate import PostingGate
from aitaolun.service import AitaolunService
from aitaolun.state import StateStore
from aitaolun.tools import SnapshotTool, build_tools

ME = "测试机"
TID = "b" * 24
IMG = "/img/" + "a" * 24 + ".webp"
AVATAR = "/avatar/v1/" + "a" * 24 + ".webp"
SITE = snap.SITE_ORIGIN


def run(coro):
    return asyncio.run(coro)


# ------------------------------------------------------------------- escaping


def test_escaping_closes_both_the_html_and_the_jinja_hole():
    out = snap.esc('<b>x</b> & "q" {{ 7 }} {% raw %}' + chr(0))
    assert "&lt;b&gt;" in out
    assert "&amp;" in out
    assert "&quot;" in out
    assert "&#123;&#123;" in out
    # A brace surviving to the remote renderer would be evaluated as a template.
    assert "{" not in out and "}" not in out
    assert chr(0) not in out
    assert snap.esc(None) == ""


def test_rendered_pages_carry_no_braces_past_the_stylesheet():
    page = snap.build_thread_html(thread_payload(), ME)
    assert page.startswith(
        '<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">'
    )
    assert "</head>" in page
    assert page.count("<style>") == 1
    body = page.split("</style>", 1)[-1]
    assert "{" not in body and "}" not in body
    assert page.endswith("</div></body></html>")


def test_inline_markdown_renders_the_pieces_it_claims_to():
    raw = (
        "**粗** *斜* ~~删~~ "
        + snap._TICK
        + "code()"
        + snap._TICK
        + " [站点](https://aitaolun.net) @别人 ![图说]("
        + IMG
        + ")"
    )
    out = snap._inline(snap.esc(raw))
    assert "<b>粗</b>" in out
    assert "<i>斜</i>" in out
    assert "<s>删</s>" in out
    assert "<code>code()</code>" in out
    assert '<span class="link">站点</span>' in out
    assert '<span class="at">@别人</span>' in out
    assert '<img class="post-img"' in out
    assert 'src="' + SITE + IMG + '"' in out
    assert "](" not in out and "**" not in out and "~~" not in out


def test_a_bare_site_path_becomes_a_figure_too():
    out = snap._inline(snap.esc("看这个 " + IMG))
    assert '<img class="post-img"' in out
    assert '<span class="cap">站内图</span>' in out


def test_images_only_render_when_they_are_real_site_files():
    assert "post-img" in snap._image_tag(IMG, "说明", True)
    off = snap._image_tag(IMG, "说明", False)
    assert "imgref" in off and "[图]" in off
    for bad in ("data:image/png;base64,AAAA", "img/relative.webp", "", None):
        assert "imgref" in snap._image_tag(bad, "", True)
    assert snap._abs_url("/x") == SITE + "/x"
    assert snap._abs_url("https://e.com/a.png") == "https://e.com/a.png"
    assert snap._abs_url("") == ""
    assert snap._abs_url("a/b") == ""


def test_block_markdown_is_a_forgiving_state_machine():
    body = (
        "# 标题\n\n一段话\n第二行\n\n- 甲\n- 乙\n\n1. 一\n2. 二\n\n> 引用\n\n---\n\n"
        + snap._FENCE
        + "\nprint(1)\n"
        + snap._FENCE
    )
    out = snap._blocks(body)
    assert "<h2>标题</h2>" in out
    assert "<p>一段话<br>第二行</p>" in out
    # Ordered lists deliberately reuse ul: the numbers are cosmetic here.
    assert out.count("<ul>") == 2 and "<ol>" not in out
    assert "<blockquote>引用</blockquote>" in out
    assert "<hr>" in out
    assert "<pre>print(1)</pre>" in out


def test_an_empty_body_still_produces_a_visible_cell():
    for value in ("", "   ", None):
        assert snap._blocks(value) == '<p class="muted">（空正文）</p>'


def test_a_body_is_truncated_rather_than_allowed_to_run_off_the_page():
    out = snap._blocks("x" * (snap.BODY_LIMIT + 500))
    assert len(out) < snap.BODY_LIMIT + 200
    assert "…" in out


def test_avatar_falls_back_to_a_stable_tile():
    tile = snap._avatar_html(ME, "", True)
    assert 'class="avatar tile"' in tile
    assert snap._avatar_html(ME, "", True) == tile
    assert 'class="avatar tile"' in snap._avatar_html(ME, IMG, False)
    real = snap._avatar_html(ME, IMG, True)
    assert real.startswith('<img class="avatar"')
    assert SITE + AVATAR in real
    assert 'class="avatar tile"' in snap._avatar_html("", "", True)


def test_avatar_of_digs_the_url_out_of_whatever_key_the_api_used():
    for key in snap._AVATAR_KEYS:
        assert snap.avatar_of({key: IMG}) == IMG, key
    # /search spells an agent hit with a nested author object.
    assert snap.avatar_of({"author": {"avatar_url": IMG}}) == IMG
    # A name we have never seen still beats falling back to a letter tile.
    assert snap.avatar_of({"writer_Avatar_thing": IMG}) == IMG
    assert snap.avatar_of({"author_avatar": "  " + IMG + "  "}) == IMG
    for nothing in ({}, {"author_avatar": ""}, {"author": "别人"}, None, "别人", []):
        assert snap.avatar_of(nothing) == ""


def test_avatar_url_is_rewritten_to_the_small_square():
    assert snap._avatar_variant(IMG) == AVATAR
    assert snap._avatar_variant(AVATAR) == AVATAR
    for untouched in ("https://cdn.example/a.png", "/img/notahexid.webp", "/img/x"):
        assert snap._avatar_variant(untouched) == untouched
    for blank in ("", None, "   "):
        assert snap._avatar_variant(blank) == ""


def test_short_id_keeps_only_the_recognisable_tail():
    assert snap._short_id("0" * 18 + "123abc") == "#123abc"
    assert snap._short_id("") == ""
    assert snap._short_id(None) == ""
    assert snap._short_id("shuiba") == "shuiba"
    assert len(snap._short_id("x" * 40)) <= 14


# -------------------------------------------------------------- shared payloads


def floors(count, author="别人"):
    return [
        {
            "number": index,
            "id": "%024d" % index,
            "author_name": author,
            "body": "第 %d 层" % index,
        }
        for index in range(1, count + 1)
    ]


def thread_payload(items=None, **extra):
    thread = {
        "id": TID,
        "title": "标题 {{ 7 }}",
        "bar": "shuiba",
        "author_name": ME,
        "floor_count": 3,
        "pinned": True,
        "featured": True,
    }
    thread.update(extra)
    if items is None:
        items = [
            {"number": 1, "id": "d" * 24, "author_name": ME, "body": "我先说 {% raw %}"},
            {
                "number": 2,
                "id": "e" * 24,
                "author_name": "别人",
                "body": "我反对",
                "upvotes": 3,
                "downvotes": 1,
                "subfloors": [
                    {"author_name": ME, "body": "你懂什么", "reply_to_name": "别人"}
                ],
            },
            {"number": 3, "id": "f" * 24, "author_name": "路人", "body": "路过"},
        ]
    return {"thread": thread, "floors": items}


# ------------------------------------------------------------ floor selection


def test_select_floors_accepts_what_a_person_would_actually_type():
    items = floors(30)
    picked, note = snap.select_floors(items, "", 5)
    assert [snap._floor_number(item) for item in picked] == [1, 2, 3, 4, 5]
    assert "只画了前 5 层" in note and "共 30 层" in note

    for spec in ("last", "最新"):
        picked, note = snap.select_floors(items, spec, 3)
        assert [snap._floor_number(item) for item in picked] == [28, 29, 30]
        assert "只画了最后 3 层" in note

    picked, note = snap.select_floors(items, "全部")
    assert len(picked) == 30 and note == ""

    assert [snap._floor_number(i) for i in snap.select_floors(items, "2-4")[0]] == [2, 3, 4]
    assert [snap._floor_number(i) for i in snap.select_floors(items, "1,3,7")[0]] == [1, 3, 7]
    assert [snap._floor_number(i) for i in snap.select_floors(items, "6至4")[0]] == [4, 5, 6]
    assert [snap._floor_number(i) for i in snap.select_floors(items, "3、7")[0]] == [3, 7]


def test_an_unreadable_floor_spec_degrades_and_says_so():
    items = floors(30)
    picked, note = snap.select_floors(items, "第三层谢谢", 5)
    assert len(picked) == 5 and "看不懂" in note
    picked, note = snap.select_floors(items, "999", 5)
    assert len(picked) == 5 and "没有匹配" in note


def test_selecting_from_nothing_is_not_an_error():
    assert snap.select_floors([], "all") == ([], "")
    assert snap.select_floors([], "") == ([], "")


def test_all_is_still_capped_so_a_1000_floor_thread_cannot_be_drawn():
    picked, note = snap.select_floors(floors(199), "all")
    assert len(picked) == snap.HARD_MAX_FLOORS
    assert "只画到第 " + str(snap.HARD_MAX_FLOORS) + " 层" in note


def test_floor_numbers_mean_platform_numbers_not_list_offsets():
    items = [{"number": number, "id": "%024d" % number} for number in (11, 12, 13)]
    assert [snap._floor_number(i) for i in snap.select_floors(items, "12")[0]] == [12]
    assert "没有匹配" in snap.select_floors(items, "1")[1]


def test_floors_that_state_no_number_are_counted_the_way_the_site_counts():
    items = [{"id": "%024d" % n, "body": "第 %d 条" % n} for n in (1, 2, 3)]
    # floor_count == replies + 1: the opening post is 1 楼, so replies start at 2.
    table = snap.resolve_floor_numbers({"floor_count": 4}, items)
    assert [table[id(item)] for item in items] == [2, 3, 4]
    # Nothing to go on: count from 1 rather than print "? 楼" three times.
    assert [snap.resolve_floor_numbers({}, items)[id(i)] for i in items] == [1, 2, 3]
    # A partial read is not the root-apart case and must not be shifted.
    assert [snap.resolve_floor_numbers({"floor_count": 40}, items)[id(i)] for i in items] == [1, 2, 3]
    # The payload itself is never written to.
    assert all("number" not in item for item in items)


def test_stated_floor_numbers_win_unless_they_are_plainly_list_offsets():
    stated = [{"number": n, "id": "%024d" % n} for n in (7, 8, 9)]
    table = snap.resolve_floor_numbers({"floor_count": 4}, stated)
    assert [table[id(item)] for item in stated] == [7, 8, 9]
    # A reply cannot be 1 楼 when the opening post is one: that is a list index.
    offsets = [{"number": n, "id": "%024d" % n} for n in (1, 2, 3)]
    shifted = snap.resolve_floor_numbers({"floor_count": 4}, offsets)
    assert [shifted[id(item)] for item in offsets] == [2, 3, 4]
    assert snap.resolve_floor_numbers({}, []) == {}


# ------------------------------------------------------------ target guessing


def test_parse_target_recognises_urls_ids_and_plain_words():
    assert snap.parse_target(SITE + "/t/" + TID) == ("thread", TID)
    assert snap.parse_target("看看 " + SITE + "/b/shuiba 里有什么") == ("feed", "shuiba")
    assert snap.parse_target(SITE + "/u/%E6%B5%8B%E8%AF%95%E6%9C%BA") == ("profile", ME)
    assert snap.parse_target(TID.upper()) == ("thread", TID)
    assert snap.parse_target("/b/二次元") == ("feed", "二次元")
    for blank in ("", "   ", None):
        assert snap.parse_target(blank) == ("", "")
    assert snap.parse_target("有什么好玩的") == ("", "有什么好玩的")


# --------------------------------------------------------------- thread view


def test_thread_view_marks_my_own_floors_and_the_asked_for_highlight():
    page = snap.build_thread_html(thread_payload(), ME, "", "2", 12, True)
    assert page.count('class="card floor hl"') == 1
    # Floor 1 plus my subfloor; the thread author line deliberately has no badge.
    assert page.count('class="badge me"') == 2
    for needle in ("置顶", "加精", "赞 3", "踩 1", "shuiba 吧"):
        assert needle in page, needle
    assert SITE + "/t/" + TID in page
    assert "#" + "e" * 6 in page


def test_a_floor_id_works_as_a_highlight_key_too():
    page = snap.build_thread_html(thread_payload(), ME, "", "E" * 24)
    assert page.count('class="card floor hl"') == 1
    assert 'class="card floor hl">' in page


def test_subfloors_never_embed_an_image_even_when_embedding_is_on():
    items = [
        {
            "number": 1,
            "id": "d" * 24,
            "author_name": ME,
            "body": "![楼层](" + IMG + ")",
            "subfloors": [{"author_name": "别人", "body": "![楼中楼](" + IMG + ")"}],
        }
    ]
    page = snap.build_thread_html(thread_payload(items), ME)
    assert page.count('<img class="post-img"') == 1
    assert "[图] 楼中楼" in page


def test_thread_view_caps_the_subfloor_list():
    subs = [
        {"author_name": "路人", "body": "第 %d 条" % index}
        for index in range(snap.MAX_SUBFLOORS + 3)
    ]
    items = [{"number": 1, "id": "d" * 24, "author_name": ME, "body": "正文", "sub_floors": subs}]
    page = snap.build_thread_html(thread_payload(items), ME)
    assert "还有 3 条楼中楼没画" in page


def test_thread_view_says_so_when_there_is_nothing_to_draw():
    assert "没有取到楼层" in snap.build_thread_html(thread_payload([]), ME)
    assert snap.build_thread_html(None, ME)


def test_thread_view_draws_the_opening_post_the_floor_list_leaves_out():
    page = snap.build_thread_html(thread_payload(body="我先问一句"), ME)
    assert "我先问一句" in page
    assert '<span class="badge tag">楼主</span>' in page
    # No body to show, nothing to draw: the header already names the author.
    assert '<span class="badge tag">楼主</span>' not in snap.build_thread_html(
        thread_payload(), ME
    )


def test_the_opening_post_is_not_drawn_twice_when_the_payload_includes_it():
    data = thread_payload(
        [{"id": TID, "author_name": ME, "body": "我先问一句"}], body="我先问一句"
    )
    assert snap.build_thread_html(data, ME).count("我先问一句") == 1


def test_thread_view_never_prints_an_unknown_floor_number():
    items = [
        {"id": "%024d" % n, "author_name": "别人", "body": "第 %d 条" % n}
        for n in (1, 2, 3)
    ]
    page = snap.build_thread_html(thread_payload(items, floor_count=4), ME)
    assert "? 楼" not in page
    for label in ("2 楼", "3 楼", "4 楼"):
        assert label in page, label


def test_the_text_thread_view_shows_the_opening_post_and_real_floor_numbers():
    text = fmt.fmt_thread(thread_payload(body="我先问一句"), me=ME)
    assert "我先问一句" in text
    assert "1 楼" in text and "（楼主）" in text
    counted = fmt.fmt_thread(
        {
            "thread": {"id": TID, "title": "标题", "floor_count": 4},
            "floors": [
                {"id": "%024d" % n, "author_name": "别人", "body": "第 %d 条" % n}
                for n in (1, 2, 3)
            ],
        }
    )
    assert "? 楼" not in counted
    for label in ("2 楼", "3 楼", "4 楼"):
        assert label in counted, label


# ----------------------------------------------------------------- feed view


def test_feed_view_flags_my_own_threads_and_keeps_the_scope_visible():
    data = {
        "threads": [
            {
                "id": "d" * 24,
                "title": "我的帖",
                "bar": "shuiba",
                "author_name": ME,
                "heat": 42,
                "pinned": True,
                "floor_count": 3,
            },
            {
                "id": "e" * 24,
                "title": "别人的帖",
                "bar": "shuiba",
                "author_name": "别人",
                "excerpt": "摘要在这里",
                "featured": True,
            },
        ]
    }
    page = snap.build_feed_html(data, ME, "shuiba", 20, True)
    for needle in ("你开的", "热度 42", "摘要在这里", "shuiba 吧", "置顶", "加精", "共 2 条"):
        assert needle in page, needle
    assert SITE + "/b/shuiba" in page


def test_feed_view_admits_when_it_only_drew_the_top_of_the_list():
    data = {"threads": [{"id": "%024d" % n, "title": "帖 %d" % n} for n in range(5)]}
    page = snap.build_feed_html(data, ME, "", 2)
    assert page.count('class="card row"') == 2
    assert "画了前 2 条" in page


def test_an_empty_feed_never_invents_a_heat_number():
    page = snap.build_feed_html({"threads": []}, ME, "")
    assert "这里现在是空的" in page
    assert "全站信息流" in page
    assert "热度 " not in page
    assert "这里现在是空的" in snap.build_feed_html({}, ME, "")


# -------------------------------------------------------------- profile view


def test_profile_view_flattens_the_me_envelope():
    data = {
        "agent": {
            "name": ME,
            "claimed": True,
            "bio": "一个用 AstrBot 跑的 agent。",
            "signature": "一句签名",
            "framework": "AstrBot",
            "level": 2,
        },
        "quota": {"threads": 1, "floors_left": 4},
        "stats": {"reputation": 7},
    }
    page = snap.build_profile_html(data)
    for needle in (ME, "已认领", "一个用 AstrBot 跑的 agent。", "一句签名", "主题 1", "楼层剩余 4", "reputation 7"):
        assert needle in page, needle
    assert SITE + "/u/" + ME in page


def test_profile_view_also_reads_the_flat_agent_shape():
    data = {
        "name": ME,
        "claimed": False,
        "bio": "",
        "bars": [{"slug": "shuiba"}, {"name": "二次元"}],
        "bans": [{"reason": "刷屏"}],
    }
    page = snap.build_profile_html(data)
    for needle in ("未认领", "拥有的吧", "shuiba", "二次元", "封禁记录", "(空)"):
        assert needle in page, needle


def test_profile_view_survives_a_shape_it_has_never_seen():
    for data in (None, [], {"agent": "not a dict"}):
        assert snap.build_profile_html(data)


# --------------------------------------------------------------- search view


def test_search_view_reads_the_three_real_result_buckets():
    data = {
        "threads": [{"id": "d" * 24, "title": "命中的帖", "bar": "shuiba", "author_name": "别人"}],
        "bars": [
            {
                "slug": "shuiba",
                "name": "水吧",
                "category": "chat",
                "thread_count": 9,
                "owner_name": "某人",
            }
        ],
        "agents": [{"name": "别人", "bio": "一句话简介"}],
    }
    page = snap.build_search_html(data, "关键词")
    for needle in ("主题 1 条", "吧 1 个", "AI 1 个", "水吧", "主题 9", "一句话简介", "关键词"):
        assert needle in page, needle


def test_search_view_does_not_invent_a_results_key():
    assert "没有命中结果" in snap.build_search_html({"results": [{"title": "x"}]}, "q")
    assert "没有命中结果" in snap.build_search_html({}, "q")


# --------------------------------------------------------- notification view


def test_notification_view_digs_the_excerpt_out_of_the_payload():
    data = {
        "notifications": [
            {"id": "d" * 24, "type": "reply", "from_name": "别人", "payload": {"body": "正文字段在这"}},
            {"id": "e" * 24, "type": "mention", "from_name": "路人", "payload": {"title": "标题字段在这"}},
            {"id": "f" * 24, "type": "什么鬼", "from_name": "未知"},
        ]
    }
    page = snap.build_notifications_html(data)
    for needle in ("有人回你", "正文字段在这", "被 @ 了", "标题字段在这", "什么鬼", "共 3 条"):
        assert needle in page, needle


def test_notification_text_comes_from_the_payload_not_the_top_level():
    data = {
        "notifications": [
            {
                "id": "d" * 24,
                "type": "reply",
                "from_name": "别人",
                "body": "顶层正文不该出现",
                "payload": {"thread_id": TID},
            }
        ]
    }
    assert "顶层正文不该出现" not in snap.build_notifications_html(data)


def test_an_empty_notification_list_says_exactly_that():
    assert "没有未读通知" in snap.build_notifications_html({})
    assert "没有未读通知" in snap.build_notifications_html({"items": []})


# ---------------------------------------------------------------- fake plumbing


class FakeRender:
    """Stand-in for AstrBot's html_render / text_to_image coroutines."""

    def __init__(self, result="", error=None):
        self.result = result
        self.error = error
        self.calls = []

    async def __call__(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        if self.error is not None:
            raise self.error
        return self.result


class FakeDocs:
    async def fetch(self, name, force=True):
        text = "闸门：说人话。"
        return DocPage(
            name=name,
            url="https://aitaolun.net/%s.md" % name,
            text=text,
            fetched_at=time.time(),
            revision=revision_of(text),
        )


THREAD_PAYLOAD = {
    "thread": {
        "id": TID,
        "title": "标题",
        "bar": "shuiba",
        "author_name": "别人",
        "author_avatar": IMG,
        "floor_count": 1,
    },
    "floors": [
        {
            "number": 1,
            "id": "d" * 24,
            "author_name": "别人",
            "author_avatar": IMG,
            "body": "正文",
        }
    ],
}


class ShotClient:
    """Only the read endpoints the snapshot path can reach."""

    def __init__(self, **results):
        self.calls = []
        self.results = results

    def _record(self, name, args, default):
        self.calls.append((name, args))
        outcome = self.results.get(name)
        if isinstance(outcome, Exception):
            raise outcome
        return default if outcome is None else outcome

    async def thread(self, thread_id, since_floor=None):
        return self._record(
            "thread", {"thread_id": thread_id, "since_floor": since_floor}, THREAD_PAYLOAD
        )

    async def feed(self, bar=None, limit=None):
        return self._record(
            "feed", {"bar": bar, "limit": limit}, {"threads": [dict(THREAD_PAYLOAD["thread"])]}
        )

    async def me(self):
        return self._record("me", {}, {"agent": {"name": ME, "claimed": True, "bio": "简介"}})

    async def agent(self, name):
        return self._record("agent", {"name": name}, {"name": name, "bio": "他的简介"})

    async def search(self, query, kind="all"):
        return self._record(
            "search",
            {"query": query, "kind": kind},
            {"threads": [dict(THREAD_PAYLOAD["thread"])]},
        )

    async def notifications(self, unread=True, since=None):
        return self._record(
            "notifications",
            {"unread": unread, "since": since},
            {
                "notifications": [
                    {
                        "id": "d" * 24,
                        "type": "reply",
                        "from_name": "别人",
                        "from_avatar": IMG,
                        "payload": {"body": "回你"},
                    }
                ]
            },
        )

    def names(self):
        return [item[0] for item in self.calls]


def build_service(tmp_path, client=None, *, renderer="ok", options=None, with_key=True):
    store = StateStore(data_dir=tmp_path)
    if with_key:
        store.set_api_key("atl_" + "k" * 40, ME)
    docs = FakeDocs()
    if renderer == "ok":
        renderer = snap.SnapshotRenderer(html_render=FakeRender("C:/tmp/shot.jpg"))
    elif renderer == "dead":
        renderer = snap.SnapshotRenderer(
            html_render=FakeRender(error=RuntimeError("远端挂了")),
            text_to_image=FakeRender(error=RuntimeError("本机也挂了")),
        )
    service = AitaolunService(
        client=client or ShotClient(),
        store=store,
        gate=PostingGate(docs=docs, enforce=False),
        docs=docs,
        options=dict(options or {}),
        renderer=renderer,
    )
    return service, store


def expect_guard(callable_, *, contains=""):
    try:
        run(callable_())
    except AitaolunGuardError as error:
        if contains:
            assert contains in str(error), str(error)
        return str(error)
    raise AssertionError("expected a local guard refusal")


# ------------------------------------------------------------- render ladder


def test_renderer_prefers_html_and_passes_jpeg_options():
    html_call = FakeRender("C:/tmp/a.jpg")
    t2i = FakeRender("C:/tmp/b.jpg")
    renderer = snap.SnapshotRenderer(html_render=html_call, text_to_image=t2i, quality=77)
    assert run(renderer.render("<html></html>", "文字")) == ("C:/tmp/a.jpg", "html", "")
    assert t2i.calls == []
    _, kwargs = html_call.calls[0]
    assert kwargs["return_url"] is False
    options = kwargs["options"]
    assert options["type"] == "jpeg"
    assert options["quality"] == 77
    assert options["full_page"] is True
    # Playwright rejects an unknown option and the whole render dies with it.
    assert "wait_until" not in options


def test_renderer_falls_back_to_text_to_image():
    renderer = snap.SnapshotRenderer(
        html_render=FakeRender(error=RuntimeError("远端挂了")),
        text_to_image=FakeRender("C:/tmp/b.jpg"),
    )
    path, engine, note = run(renderer.render("<html></html>", "文字"))
    assert path == "C:/tmp/b.jpg" and engine == "t2i"
    assert "网页渲染失败" in note and "RuntimeError" in note and "已降级" in note


def test_renderer_gives_up_without_losing_the_caller():
    renderer = snap.SnapshotRenderer(
        html_render=FakeRender(error=RuntimeError("远端挂了")),
        text_to_image=FakeRender(error=RuntimeError("本机也挂了")),
    )
    path, engine, note = run(renderer.render("<html></html>", "文字"))
    assert (path, engine) == ("", "none")
    assert "文字转图也失败" in note


def test_renderer_without_any_backend_says_so():
    path, engine, note = run(snap.SnapshotRenderer().render("<html></html>", "文字"))
    assert (path, engine) == ("", "none")
    assert "没有可用的渲染后端" in note


def test_renderer_honours_the_off_switch():
    renderer = snap.SnapshotRenderer(html_render=FakeRender("C:/tmp/a.jpg"), enabled=False)
    path, engine, note = run(renderer.render("<html></html>", "文字"))
    assert (path, engine) == ("", "none")
    assert "被关掉" in note
    assert renderer.html_render.calls == []


def test_renderer_clamps_a_silly_quality():
    for given, wanted in ((0, 92), (5, 30), (500, 100)):
        html_call = FakeRender("C:/tmp/a.jpg")
        renderer = snap.SnapshotRenderer(html_render=html_call, quality=given)
        run(renderer.render("<html></html>"))
        assert html_call.calls[0][1]["options"]["quality"] == wanted, given


def test_renderer_notes_an_empty_html_result():
    renderer = snap.SnapshotRenderer(
        html_render=FakeRender(""), text_to_image=FakeRender("C:/tmp/b.jpg")
    )
    path, engine, note = run(renderer.render("<html></html>", "文字"))
    assert (path, engine) == ("C:/tmp/b.jpg", "t2i")
    assert "没有返回文件" in note


def test_renderer_does_not_call_text_to_image_without_text():
    t2i = FakeRender("C:/tmp/b.jpg")
    renderer = snap.SnapshotRenderer(
        html_render=FakeRender(error=RuntimeError("远端挂了")), text_to_image=t2i
    )
    path, engine, _ = run(renderer.render("<html></html>", ""))
    assert (path, engine) == ("", "none")
    assert t2i.calls == []


# ------------------------------------------------------------ service routing


def test_snapshot_can_be_switched_off_without_touching_the_network(tmp_path):
    client = ShotClient()
    service, _ = build_service(tmp_path, client, options={"snapshot_enabled": False})
    expect_guard(lambda: service.snapshot("feed", ""), contains="snapshot_enabled")
    assert client.calls == []


def test_snapshot_without_a_renderer_refuses_early(tmp_path):
    client = ShotClient()
    service, _ = build_service(tmp_path, client, renderer=None)
    expect_guard(lambda: service.snapshot("feed", ""), contains="渲染")
    assert client.calls == []


def test_snapshot_routes_by_what_was_pasted(tmp_path):
    client = ShotClient()
    service, _ = build_service(tmp_path, client)
    assert run(service.snapshot("auto", SITE + "/t/" + TID)).view == "thread"
    assert run(service.snapshot("auto", "")).view == "feed"
    assert run(service.snapshot("auto", "有什么好玩的")).view == "search"
    assert run(service.snapshot("auto", "/b/shuiba")).view == "feed"
    assert run(service.snapshot("auto", SITE + "/u/%E6%B5%8B%E8%AF%95%E6%9C%BA")).view == "profile"
    assert run(service.snapshot("notifications", "")).view == "notifications"
    assert client.names() == ["thread", "feed", "search", "feed", "me", "notifications"]


def test_explicit_view_loses_to_a_pasted_thread_url(tmp_path):
    client = ShotClient()
    service, _ = build_service(tmp_path, client)
    assert run(service.snapshot("feed", SITE + "/t/" + TID)).view == "thread"
    assert client.names() == ["thread"]


def test_search_view_keeps_a_keyword_even_when_a_view_is_forced(tmp_path):
    client = ShotClient()
    service, _ = build_service(tmp_path, client)
    # Only thread/feed/profile lose to a URL; "search this id" is a real request.
    assert run(service.snapshot("search", SITE + "/t/" + TID)).view == "search"
    assert client.calls[0][1]["query"] == TID


def test_thread_snapshot_demands_a_real_id(tmp_path):
    client = ShotClient()
    service, _ = build_service(tmp_path, client)
    expect_guard(lambda: service.snapshot("thread", "水吧"), contains="24")
    assert client.calls == []


def test_an_unknown_view_is_rejected(tmp_path):
    service, _ = build_service(tmp_path)
    expect_guard(lambda: service.snapshot("timeline", ""), contains="view")


def test_search_snapshot_needs_a_keyword(tmp_path):
    service, _ = build_service(tmp_path)
    expect_guard(lambda: service.snapshot("search", ""), contains="关键词")


def test_snapshot_needs_a_credential(tmp_path):
    client = ShotClient()
    service, _ = build_service(tmp_path, client, with_key=False)
    try:
        run(service.snapshot("feed", ""))
    except AitaolunConfigError as error:
        assert "api_key" in str(error)
    else:
        raise AssertionError("a missing api_key must stop the read")
    assert client.calls == []


def test_a_render_failure_still_answers_and_is_recorded(tmp_path):
    service, store = build_service(tmp_path, renderer="dead")
    result = run(service.snapshot("feed", ""))
    assert not result.has_image
    assert result.engine == "none"
    assert result.text
    assert "网页渲染失败" in result.note
    assert store.runs(5)[0].status == "render_failed"


def test_a_successful_snapshot_is_recorded_with_the_engine(tmp_path):
    client = ShotClient()
    service, store = build_service(tmp_path, client)
    result = run(service.snapshot("thread", TID, "看这个"))
    assert result.has_image and result.engine == "html"
    assert result.caption.startswith("主题《标题》")
    assert "【" not in result.caption
    assert "看这个" in result.caption
    assert SITE + "/t/" + TID in result.caption
    record = store.runs(5)[0]
    assert record.trigger == "snapshot" and record.status == "ok"
    assert "thread" in record.detail and "html" in record.detail


def test_snapshot_options_control_floor_count_and_embedding(tmp_path):
    items = [
        {"number": n, "id": "%024d" % n, "author_name": "别人", "body": "![图](" + IMG + ")"}
        for n in range(1, 7)
    ]
    client = ShotClient(
        thread={"thread": dict(THREAD_PAYLOAD["thread"], floor_count=6), "floors": items}
    )
    html_call = FakeRender("C:/tmp/a.jpg")
    service, _ = build_service(
        tmp_path,
        client,
        renderer=snap.SnapshotRenderer(html_render=html_call),
        options={"snapshot_max_floors": 2, "snapshot_embed_images": "false"},
    )
    run(service.snapshot("thread", TID))
    page = html_call.calls[0][0][0]
    assert page.count('class="card floor') == 2
    assert '<img class="post-img"' not in page
    assert "只画了前 2 层" in page


def test_limit_is_passed_through_to_the_feed_call(tmp_path):
    client = ShotClient()
    service, _ = build_service(tmp_path, client)
    run(service.snapshot("feed", "shuiba", limit=3))
    assert client.calls[0][1] == {"bar": "shuiba", "limit": 3}
    run(service.snapshot("feed", "shuiba", limit=0))
    assert client.calls[1][1] == {"bar": "shuiba", "limit": 1}


def test_taking_a_picture_of_a_thread_also_records_who_spoke(tmp_path):
    client = ShotClient(
        thread={
            "thread": dict(THREAD_PAYLOAD["thread"], author_name=ME),
            "floors": [{"number": 1, "id": "d" * 24, "author_name": ME, "body": "自言自语"}],
        }
    )
    service, store = build_service(tmp_path, client)
    run(service.snapshot("thread", TID))
    assert store.thread_read(TID)["self_only"] is True


def faceless_thread(*names):
    """A thread payload whose authors carry no avatar of their own."""

    return {
        "thread": {"id": TID, "title": "标题", "bar": "shuiba", "author_name": "楼主甲"},
        "floors": [
            {"id": "%024d" % index, "author_name": name, "body": "第 %d 条" % index}
            for index, name in enumerate(names, 1)
        ],
    }


def test_a_face_the_payload_forgot_is_fetched_once_and_then_remembered(tmp_path):
    client = ShotClient(
        thread=faceless_thread("路人"), agent={"name": "路人", "avatar_url": IMG}
    )
    html_call = FakeRender("C:/tmp/a.jpg")
    service, _ = build_service(
        tmp_path, client, renderer=snap.SnapshotRenderer(html_render=html_call)
    )
    run(service.snapshot("thread", TID))
    assert client.names().count("agent") == 2  # 楼主甲 and 路人
    page = html_call.calls[0][0][0]
    assert SITE + AVATAR in page
    assert 'class="avatar tile"' not in page
    # Second picture of the same thread: the faces are already known.
    run(service.snapshot("thread", TID))
    assert client.names().count("agent") == 2


def test_avatar_lookups_are_capped_so_a_long_thread_cannot_fan_out(tmp_path):
    client = ShotClient(
        thread=faceless_thread(*["路人%d" % n for n in range(30)]),
        agent={"avatar_url": IMG},
    )
    service, _ = build_service(tmp_path, client, options={"snapshot_max_floors": 40})
    run(service.snapshot("thread", TID))
    assert client.names().count("agent") == MAX_AVATAR_LOOKUPS


def test_a_refused_avatar_lookup_costs_a_tile_not_the_screenshot(tmp_path):
    client = ShotClient(
        thread=faceless_thread("路人"),
        agent=AitaolunApiError(429, "PUBLIC_RATE_LIMITED", retry_after=85),
    )
    html_call = FakeRender("C:/tmp/a.jpg")
    service, store = build_service(
        tmp_path, client, renderer=snap.SnapshotRenderer(html_render=html_call)
    )
    result = run(service.snapshot("thread", TID))
    assert result.image_path == "C:/tmp/a.jpg"
    assert 'class="avatar tile"' in html_call.calls[0][0][0]
    # Losing a face is cosmetic; being rate limited is not, and is still recorded.
    assert store.cooldown("public_write") is not None


def test_nothing_is_fetched_for_faces_that_will_not_be_drawn(tmp_path):
    client = ShotClient(thread=faceless_thread("路人"))
    service, _ = build_service(
        tmp_path, client, options={"snapshot_embed_images": "false"}
    )
    run(service.snapshot("thread", TID))
    assert "agent" not in client.names()


# ------------------------------------------------------------------- the tool


class FakeEvent:
    def __init__(self, error=None):
        self.sent = []
        self.error = error

    async def send(self, chain):
        if self.error is not None:
            raise self.error
        self.sent.append(chain)


def wrap(event):
    return types.SimpleNamespace(context=types.SimpleNamespace(event=event))


def snapshot_tool(service):
    return [tool for tool in build_tools(service) if tool.name == "atl_snapshot"][0]


def test_the_snapshot_tool_sends_a_picture_and_tells_the_model_to_stop(tmp_path):
    service, _ = build_service(tmp_path)
    event = FakeEvent()
    text = run(snapshot_tool(service).call(wrap(event), view="feed", target="", limit=None, bogus="x"))
    assert len(event.sent) == 1
    assert "不要再把内容复述一遍" in text
    assert "html" in text


def test_the_snapshot_tool_falls_back_to_text_without_an_event(tmp_path):
    service, _ = build_service(tmp_path)
    text = run(snapshot_tool(service).call(None, view="feed"))
    assert "文字版" in text
    assert "拿不到当前会话" in text


def test_the_snapshot_tool_reports_a_send_failure(tmp_path):
    service, _ = build_service(tmp_path)
    text = run(snapshot_tool(service).call(wrap(FakeEvent(error=RuntimeError("平台掉线"))), view="feed"))
    assert "发送失败" in text and "RuntimeError" in text and "文字版" in text


def test_the_snapshot_tool_turns_guard_errors_into_text(tmp_path):
    service, _ = build_service(tmp_path, options={"snapshot_enabled": False})
    text = run(snapshot_tool(service).call(wrap(FakeEvent()), view="feed"))
    assert text.startswith("【爱讨论】")
    assert "snapshot_enabled" in text


def test_the_snapshot_tool_survives_an_unexpected_crash():
    class Boom:
        async def snapshot(self, **kwargs):
            raise RuntimeError("boom")

    tool = SnapshotTool(
        name="atl_snapshot",
        description="d",
        parameters={"type": "object", "properties": {"view": {"type": "string"}}},
        method="snapshot",
        service=Boom(),
    )
    text = run(tool.call(None, view="feed"))
    assert "截图出错" in text and "RuntimeError" in text


def test_the_snapshot_tool_without_a_service_says_so():
    assert "尚未初始化" in run(snapshot_tool(None).call(None, view="feed"))
