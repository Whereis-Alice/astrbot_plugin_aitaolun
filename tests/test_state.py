"""Private state: atomic files, cooldowns, the ban latch and memory."""

import json
import os
import stat
import sys
import time

from aitaolun.state import (
    MAX_RUNS,
    Credentials,
    RunRecord,
    StateStore,
    mask_key,
)

IMG = "/img/" + "a" * 24 + ".webp"


def store_at(tmp_path):
    return StateStore(data_dir=tmp_path)


def test_mask_key_never_leaks_the_secret():
    key = "atl_" + "s" * 40
    masked = mask_key(key)
    assert key not in masked
    assert masked.startswith("atl_")
    assert masked.endswith("(len=44)")
    assert mask_key("") == "(未设置)"
    assert mask_key("short") == "sh***"


def test_credentials_roundtrip_and_public_view(tmp_path):
    store = store_at(tmp_path)
    assert not store.credentials().has_key

    store.set_api_key("atl_" + "k" * 40, "小讨论")
    reloaded = store_at(tmp_path).credentials()
    assert reloaded.has_key
    assert reloaded.agent_name == "小讨论"
    assert reloaded.registered_at > 0

    public = json.dumps(reloaded.public_dict(), ensure_ascii=False)
    assert "k" * 40 not in public

    store.clear_credentials()
    assert not store_at(tmp_path).credentials().has_key


def test_credential_file_is_private_and_written_atomically(tmp_path):
    store = store_at(tmp_path)
    store.save_credentials(Credentials(api_key="atl_x", agent_name="n"))
    path = store.credentials_path
    assert path.is_file()
    # No leftover temp file from the atomic replace.
    assert not list(tmp_path.glob("*.tmp"))
    if sys.platform != "win32":
        assert stat.S_IMODE(os.stat(path).st_mode) == 0o600


def test_cooldowns_expire(tmp_path):
    store = store_at(tmp_path)
    live = store.set_cooldown("public_write", 60, "PUBLIC_RATE_LIMITED")
    assert live.remaining > 0
    assert store.cooldown("public_write") is not None
    assert [item.kind for item in store.active_cooldowns()] == ["public_write"]

    store.runtime()["cooldowns"]["public_write"]["until"] = time.time() - 1
    assert store.cooldown("public_write") is None
    assert store.active_cooldowns() == []

    store.set_cooldown("image", 30)
    store.clear_cooldown("image")
    assert store.cooldown("image") is None


def test_duplicate_guard_only_fires_across_targets(tmp_path):
    store = store_at(tmp_path)
    store.record_write("thread", "shuiba", "fp1", "id1")

    # Same target + same content is a legal exact retry.
    assert store.find_cross_target_duplicate("thread", "shuiba", "fp1") is None
    # Different content, same target: unrelated.
    assert store.find_cross_target_duplicate("thread", "shuiba", "fp2") is None
    # Different kind: unrelated.
    assert store.find_cross_target_duplicate("floor", "other", "fp1") is None

    hit = store.find_cross_target_duplicate("thread", "otherba", "fp1")
    assert hit is not None
    assert hit.target == "shuiba"
    assert hit.result_id == "id1"


def test_expired_fingerprints_are_pruned(tmp_path):
    store = store_at(tmp_path)
    store.record_write("thread", "shuiba", "fp1")
    store.runtime()["fingerprints"][0]["created_at"] = time.time() - 25 * 3600
    assert store.find_cross_target_duplicate("thread", "otherba", "fp1") is None


def test_platform_ban_latch_survives_reload(tmp_path):
    store = store_at(tmp_path)
    assert store.platform_ban() is None
    store.set_platform_banned(True, "刷屏")

    fresh = store_at(tmp_path)
    ban = fresh.platform_ban()
    assert ban is not None and ban["reason"] == "刷屏"

    fresh.set_platform_banned(False)
    assert store_at(tmp_path).platform_ban() is None


def test_memory_overwrite_append_and_unknown_section(tmp_path):
    store = store_at(tmp_path)
    store.write_memory("persona", "我是个爱吵架的家伙")
    assert store.write_memory("persona", "但也讲道理", append=True).endswith("但也讲道理")
    assert store.write_memory("persona", "重来", append=False) == "重来"

    sections = store_at(tmp_path).read_memory()
    assert sections["persona"] == "重来"
    assert sections["relations"] == ""
    assert set(store.read_memory("bars")) == {"bars"}
    assert store.memory_updated_at()["persona"] > 0

    for bad in ("nope", ""):
        try:
            store.write_memory(bad, "x")
        except KeyError:
            pass
        else:
            raise AssertionError("unknown memory section must raise")


def test_runs_are_a_bounded_newest_first_ring(tmp_path):
    store = store_at(tmp_path)
    for index in range(MAX_RUNS + 5):
        store.append_run(
            RunRecord(
                started_at=time.time(),
                trigger="heartbeat",
                status="ok",
                detail="run-%d" % index,
            )
        )
    runs = store.runs()
    assert len(runs) == MAX_RUNS
    assert runs[0].detail == "run-%d" % (MAX_RUNS + 4)
    assert store.runs(limit=3)[0].detail == runs[0].detail
    assert len(store.runs(limit=3)) == 3


def test_image_attribution_is_remembered_without_duplicates(tmp_path):
    store = store_at(tmp_path)
    assert not store.owns_image(IMG)
    store.record_image(IMG, "https://example.com/a.png")
    store.record_image(IMG, "https://example.com/a.png")
    assert store_at(tmp_path).owns_image(IMG)
    assert len(store.owned_images()) == 1


def test_session_binding_and_scheduler_state(tmp_path):
    store = store_at(tmp_path)
    assert store.bound_session() == ""
    store.bind_session("aiocqhttp:GroupMessage:123", "测试群")
    assert store_at(tmp_path).bound_session() == "aiocqhttp:GroupMessage:123"

    store.update_scheduler_state(next_at=123.0, platform="aiocqhttp")
    assert store_at(tmp_path).scheduler_state()["platform"] == "aiocqhttp"
    store.update_skill_update_state(last_at=1.0)
    assert store_at(tmp_path).skill_update_state()["last_at"] == 1.0

    store.unbind_session()
    assert store.bound_session() == ""


def test_corrupt_files_degrade_to_empty_state(tmp_path):
    store = store_at(tmp_path)
    store.runtime_path.write_text("{not json", encoding="utf-8")
    store.credentials_path.write_text("[]", encoding="utf-8")
    fresh = store_at(tmp_path)
    assert fresh.runtime()["cooldowns"] == {}
    assert not fresh.credentials().has_key
