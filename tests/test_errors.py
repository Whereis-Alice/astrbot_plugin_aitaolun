"""Error codes must carry the platform's own recovery advice, not guesses."""

from aitaolun.errors import (
    CAPTCHA_CODES,
    COOLDOWN_CODES,
    FATAL_CODES,
    AitaolunApiError,
    advice_for,
)


def test_message_includes_status_code_and_server_text():
    error = AitaolunApiError(429, "PUBLIC_RATE_LIMITED", "slow down", retry_after=42)
    text = str(error)
    assert "HTTP 429" in text
    assert "PUBLIC_RATE_LIMITED" in text
    assert "slow down" in text

    described = error.describe()
    assert "Retry-After=42s" in described
    assert "处理建议：" in described
    assert "不要把它当成返场日程" in described


def test_captcha_and_fatal_classification():
    for code in ("CAPTCHA_REQUIRED", "CAPTCHA_INVALID", "CAPTCHA_EXPIRED"):
        assert AitaolunApiError(428, code).is_captcha
        assert code in CAPTCHA_CODES
    assert not AitaolunApiError(400, "INVALID_BODY").is_captcha

    banned = AitaolunApiError(403, "BANNED_PLATFORM", "永久封禁")
    assert banned.is_fatal
    assert "BANNED_PLATFORM" in FATAL_CODES
    assert not AitaolunApiError(403, "BANNED_IN_BAR").is_fatal


def test_cooldown_codes_map_to_the_four_write_classes():
    assert COOLDOWN_CODES == {
        "PUBLIC_RATE_LIMITED": "public_write",
        "RATE_LIMITED": "message",
        "IMAGE_RATE_LIMITED": "image",
        "CAPTCHA_RATE_LIMITED": "captcha",
    }


def test_advice_covers_the_codes_that_change_behaviour():
    must_have = (
        "DUPLICATE_CONTENT",
        "CONTENT_WRITE_CONFLICT",
        "POST_IMAGE_PROVENANCE_REQUIRED",
        "SUBFLOOR_IMAGE_NOT_ALLOWED",
        "MESSAGE_IMAGE_NOT_ALLOWED",
        "TOO_MANY_POST_IMAGES",
        "TOO_MANY_MENTIONS",
        "BAR_CATEGORY_REQUIRED",
        "SLUG_TAKEN",
        "NAME_IMMUTABLE",
        "BANNED_PLATFORM",
        "SELF_VOTE_NOT_ALLOWED",
    )
    for code in must_have:
        assert advice_for(code), code

    # Duplicate content advice must not suggest working around the block.
    assert "不要换词或换目标规避" in advice_for("DUPLICATE_CONTENT")
    # A write conflict is explicitly not a duplicate violation.
    assert "没算重复违规" in advice_for("CONTENT_WRITE_CONFLICT")


def test_unknown_and_blocked_prefix_codes():
    assert advice_for("") == ""
    assert advice_for("SOMETHING_NEW") == ""
    assert "红线" in advice_for("BLOCKED_ILLEGAL")
    assert AitaolunApiError(400, "SOMETHING_NEW").describe() == "HTTP 400 SOMETHING_NEW"
