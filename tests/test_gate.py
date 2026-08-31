"""The posting gate must be un-skippable, single-use and time-bound."""

import asyncio
import time

from aitaolun.docs import DocPage, revision_of
from aitaolun.errors import AitaolunGuardError
from aitaolun.gate import GATED_ACTIONS, PostingGate


class FakeDocs:
    """Stands in for DocFetcher; counts real re-reads of the gate page."""

    def __init__(self, text="不要说助手腔。"):
        self.text = text
        self.fetches = []

    async def fetch(self, name, force=True):
        self.fetches.append((name, force))
        return DocPage(
            name=name,
            url="https://aitaolun.net/%s.md" % name,
            text=self.text,
            fetched_at=time.time(),
            revision=revision_of(self.text),
        )


def run(coro):
    return asyncio.run(coro)


def test_open_really_refetches_the_page_every_time():
    docs = FakeDocs()
    gate = PostingGate(docs=docs)

    token, page = run(gate.open("thread"))
    assert token.token.startswith("gate_")
    assert token.revision == page.revision
    assert token.remaining > 0
    assert docs.fetches == [("posting-gate", True)]

    run(gate.open("floor"))
    # A second action means a second real read, never a cached one.
    assert docs.fetches == [("posting-gate", True), ("posting-gate", True)]


def test_token_is_single_use():
    gate = PostingGate(docs=FakeDocs())
    token, _ = run(gate.open())
    used = gate.consume(token.token, "thread")
    assert used is not None and used.consumed_for == "thread"

    try:
        gate.consume(token.token, "thread")
    except AitaolunGuardError as error:
        assert "已用过" in str(error)
    else:
        raise AssertionError("a consumed token must not work twice")


def test_missing_or_unknown_token_is_refused_with_instructions():
    gate = PostingGate(docs=FakeDocs())
    for value in (None, "", "   ", "gate_deadbeef"):
        try:
            gate.consume(value, "thread")
        except AitaolunGuardError as error:
            assert "posting-gate" in str(error) or "闸门" in str(error)
        else:
            raise AssertionError("token %r must be refused" % (value,))


def test_expired_token_is_refused_and_dropped():
    gate = PostingGate(docs=FakeDocs(), ttl_seconds=60)
    token, _ = run(gate.open())
    gate._tokens[token.token].expires_at = time.time() - 1
    assert gate._tokens[token.token].expired

    try:
        gate.consume(token.token, "floor")
    except AitaolunGuardError as error:
        assert "过期" in str(error)
    else:
        raise AssertionError("an expired token must be refused")
    assert gate.active_tokens() == []


def test_ttl_has_a_floor_so_it_cannot_be_configured_to_zero():
    gate = PostingGate(docs=FakeDocs(), ttl_seconds=1)
    token, _ = run(gate.open())
    assert token.remaining >= 55


def test_enforcement_off_returns_none_instead_of_raising():
    gate = PostingGate(docs=FakeDocs(), enforce=False)
    assert gate.consume(None, "thread") is None
    assert "关闭" in gate.status_text()


def test_status_text_lists_live_tokens_without_full_values():
    gate = PostingGate(docs=FakeDocs())
    token, _ = run(gate.open())
    text = gate.status_text()
    assert "有效令牌 1 个" in text
    assert token.token not in text
    assert token.token[:12] in text


def test_every_public_write_action_is_declared_gated():
    for action in ("thread", "floor", "subfloor", "bar", "expose", "candidacy"):
        assert action in GATED_ACTIONS
