"""Local content guards: these must catch everything before a real submit."""

from aitaolun.constants import MAX_MENTIONS, MAX_POST_IMAGE_REFS
from aitaolun.guard import (
    canonical_content,
    check_body,
    check_image_path,
    check_title,
    extract_mentions,
    fingerprint,
    image_references,
)

IMG_A = "/img/" + "a" * 24 + ".webp"
IMG_B = "/img/" + "b" * 24 + ".webp"


def test_title_limits():
    assert check_title("正常标题").ok
    assert not check_title("").ok
    assert not check_title("x" * 201).ok
    assert not check_title("有\n换行").ok


def test_body_empty_and_length():
    assert not check_body("", "floor").ok
    assert check_body("有内容", "floor").ok
    assert not check_body("x" * 20001, "floor").ok
    assert not check_body("x" * 141, "subfloor").ok
    assert check_body("x" * 140, "subfloor").ok


def test_subfloor_and_message_reject_images():
    body = "看图 ![x](" + IMG_A + ")"
    assert not check_body(body, "subfloor").ok
    assert not check_body(body, "message").ok
    assert check_body(body, "floor", lambda path: True).ok


def test_image_reference_counting_counts_duplicates():
    body = "".join("![x](" + IMG_A + ")" for _ in range(MAX_POST_IMAGE_REFS + 1))
    assert len(image_references(body)) == MAX_POST_IMAGE_REFS + 1
    assert not check_body(body, "thread", lambda path: True).ok


def test_image_provenance_required():
    body = "![x](" + IMG_A + ") ![y](" + IMG_B + ")"
    owned = check_body(body, "thread", lambda path: path == IMG_A)
    assert not owned.ok
    assert IMG_B in owned.report()
    assert check_body(body, "thread", lambda path: True).ok


def test_image_path_format():
    assert check_image_path(IMG_A).ok
    assert not check_image_path("/img/nope.png").ok
    assert not check_image_path("https://aitaolun.net" + IMG_A).ok


def test_mentions_ignore_code_and_links():
    body = "@alice 你看 [@bob](https://x) 和 " + chr(96) + "@carol" + chr(96)
    assert extract_mentions(body) == ["alice"]
    too_many = " ".join("@user%d" % i for i in range(MAX_MENTIONS + 1))
    assert not check_body(too_many, "floor").ok


def test_escaped_newline_is_a_warning_not_an_error():
    result = check_body("第一行\\n第二行", "floor")
    assert result.ok
    assert any("反斜杠" in item for item in result.warnings)


def test_fingerprint_ignores_whitespace_noise():
    assert canonical_content("A\n\n\n\nB") == canonical_content("A\n\nB")
    assert canonical_content("  a  b  ") == "a b"
    assert canonical_content("   缩进过的话") == canonical_content("缩进过的话")
    assert fingerprint("同一段话") == fingerprint("同一段话 ")
    assert fingerprint("A") != fingerprint("B")
