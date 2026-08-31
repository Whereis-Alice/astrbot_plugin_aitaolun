"""TOOLS.md 必须和 aitaolun/tools.py 的注册表保持同步。"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DOC = ROOT / "TOOLS.md"
GENERATOR = ROOT / "scripts" / "gen_tool_docs.py"


def _load_generator():
    spec = importlib.util.spec_from_file_location("atl_gen_tool_docs", GENERATOR)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


gen = _load_generator()


def _text() -> str:
    return DOC.read_text(encoding="utf-8").replace("\r\n", "\n")


def test_doc_exists_and_is_not_a_stub():
    assert DOC.exists(), "TOOLS.md 不存在"
    assert len(_text()) > 4000


def test_doc_is_up_to_date_with_the_tool_registry():
    assert _text() == gen.render(), (
        "TOOLS.md 和 aitaolun/tools.py 不一致，跑 python scripts/gen_tool_docs.py 重新生成。"
    )


def test_every_tool_has_its_own_section():
    from aitaolun.tools import tool_names

    text = _text()
    for name in tool_names():
        assert f"### {name}\n" in text, f"{name} 在 TOOLS.md 里没有章节"


def test_every_documented_section_is_a_real_tool():
    from aitaolun.tools import tool_names

    known = set(tool_names())
    headings = [
        line[4:].strip() for line in _text().splitlines() if line.startswith("### ")
    ]
    assert len(headings) == len(known)
    assert set(headings) == known


def test_groups_cover_the_registry_exactly():
    from aitaolun.tools import tool_names

    grouped = [name for _, _, names in gen.GROUPS for name in names]
    assert len(grouped) == len(set(grouped)), "GROUPS 里有重复的工具"
    assert set(grouped) == set(tool_names())


def test_one_liners_and_notes_only_mention_real_tools():
    from aitaolun.tools import tool_names

    known = set(tool_names())
    assert set(gen.ONE_LINERS) == known
    assert set(gen.NOTES) <= known
    assert set(gen.PUBLIC) <= known
    assert set(gen.PARTIAL_PUBLIC) <= known
    assert set(gen.LOCAL_WRITE) <= known
    assert not (set(gen.PUBLIC) & set(gen.PARTIAL_PUBLIC))


def test_gate_and_captcha_columns_match_the_parameter_schema():
    from aitaolun.tools import _SPECS

    text = _text()
    for name, _, _, params in _SPECS:
        props = (params or {}).get("properties", {})
        # 速查表里该工具那一行
        row = next(
            line for line in text.splitlines() if line.startswith("| " + chr(96) + name + chr(96) + " |")
        )
        cells = [cell.strip() for cell in row.strip("|").split("|")]
        gate_cell, captcha_cell = cells[2], cells[3]
        if name == "atl_posting_gate":
            assert gate_cell == "发放"
        else:
            assert gate_cell == ("是" if "gate_token" in props else "—"), name
        assert captcha_cell == ("是" if "captcha_id" in props else "—"), name


def test_hard_rules_are_stated_up_front():
    head = _text()[:1200]
    assert "atl_posting_gate" in head
    assert "一次性" in head
    assert "captcha_answer" in head
