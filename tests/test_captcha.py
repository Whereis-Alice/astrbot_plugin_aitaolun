"""Local captcha solving must be right or silent - never confidently wrong."""

from aitaolun.captcha import parse_challenge, solve


def test_equals_sign_is_not_spliced_into_the_expression():
    # The classic trap: stripping "=" turns "12+5=17" into "12+517".
    answer, note = solve("12+5=17")
    assert answer == "17", note
    assert solve("12 + 5 = ?")[0] == "17"
    assert solve("请问 12 + 5 = 几")[0] == "17"


def test_chinese_wording_and_digits():
    assert solve("三加五等于多少")[0] == "8"
    assert solve("九减二是多少")[0] == "7"
    assert solve("四乘以六等于几")[0] == "24"
    assert solve("十减一等于多少")[0] == "9"


def test_full_width_and_parentheses():
    assert solve("（2＋3）×4 = ？")[0] == "20"
    assert solve("100 % 7 = ?")[0] == "2"


def test_refuses_when_not_plain_arithmetic():
    for question in ("", "下面哪个是水果：苹果 还是 桌子？", "把这句话反过来写"):
        answer, note = solve(question)
        assert answer is None
        assert note


def test_refuses_non_integer_and_division_by_zero():
    assert solve("7 / 2 = ?")[0] is None
    assert solve("5 / 0 = ?")[0] is None


def test_parse_challenge_accepts_several_response_shapes():
    flat = parse_challenge({"captcha_id": "abc", "question": "1+1"})
    assert flat.required and flat.usable and flat.question == "1+1"

    nested = parse_challenge({"captcha": {"id": "xyz", "prompt": "2+2"}})
    assert nested.captcha_id == "xyz"
    assert nested.question == "2+2"

    disabled = parse_challenge({"required": False})
    assert not disabled.required
    assert not disabled.usable
    assert "关闭" in disabled.describe()

    assert not parse_challenge(None).usable
