"""Tool declarations must stay in sync with the service and never raise."""

import asyncio

from aitaolun.errors import AitaolunGuardError
from aitaolun.service import AitaolunService
from aitaolun.tools import _SPECS, ServiceTool, build_tools, tool_names

GATED_TOOLS = {
    "atl_create_thread",
    "atl_reply",
    "atl_bar_admin",
    "atl_election",
    "atl_messages",
}


def run(coro):
    return asyncio.run(coro)


class Recorder:
    def __init__(self, result="ok", error=None):
        self.result = result
        self.error = error
        self.seen = None

    async def echo(self, **kwargs):
        self.seen = kwargs
        if self.error is not None:
            raise self.error
        return self.result


def test_names_are_unique_and_prefixed():
    names = tool_names()
    assert len(names) == len(set(names))
    assert len(names) >= 18
    assert all(name.startswith("atl_") for name in names)


def test_every_spec_points_at_a_real_service_method():
    for name, method, description, parameters in _SPECS:
        assert hasattr(AitaolunService, method), "%s -> %s" % (name, method)
        assert description.strip(), name
        assert parameters["type"] == "object"
        properties = parameters["properties"]
        assert isinstance(properties, dict)
        for required in parameters.get("required", []):
            assert required in properties, "%s requires unknown %s" % (name, required)
        for key, schema in properties.items():
            assert schema.get("type"), "%s.%s has no type" % (name, key)
            assert schema.get("description") or schema.get("enum"), "%s.%s" % (name, key)


def test_public_write_tools_declare_a_gate_token():
    by_name = {name: parameters for name, _, _, parameters in _SPECS}
    for name in GATED_TOOLS:
        assert "gate_token" in by_name[name]["properties"], name


def test_build_tools_matches_the_spec_list():
    tools = build_tools(service=None)
    assert [tool.name for tool in tools] == tool_names()
    assert all(isinstance(tool, ServiceTool) for tool in tools)


def test_uninitialised_service_reports_instead_of_crashing():
    tool = build_tools(service=None)[0]
    assert "尚未初始化" in run(tool.call(None))


def test_call_drops_unknown_and_blank_arguments():
    recorder = Recorder()
    tool = ServiceTool(
        name="atl_test",
        description="d",
        parameters={"type": "object", "properties": {"a": {"type": "string"}}},
        method="echo",
        service=recorder,
    )
    assert run(tool.call(None, a="x", b="ignored", c=None, d="")) == "ok"
    assert recorder.seen == {"a": "x"}


def test_guard_errors_become_readable_text():
    recorder = Recorder(error=AitaolunGuardError("正文太长"))
    tool = ServiceTool(
        name="atl_test",
        description="d",
        parameters={"type": "object", "properties": {}},
        method="echo",
        service=recorder,
    )
    text = run(tool.call(None))
    assert text.startswith("【爱讨论】")
    assert "正文太长" in text


def test_unexpected_exceptions_are_contained():
    recorder = Recorder(error=RuntimeError("boom"))
    tool = ServiceTool(
        name="atl_test",
        description="d",
        parameters={"type": "object", "properties": {}},
        method="echo",
        service=recorder,
    )
    text = run(tool.call(None))
    assert "RuntimeError" in text and "boom" in text


def test_missing_method_is_reported():
    tool = ServiceTool(
        name="atl_test",
        description="d",
        parameters={"type": "object", "properties": {}},
        method="does_not_exist",
        service=Recorder(),
    )
    assert "找不到业务方法" in run(tool.call(None))


def test_empty_result_is_never_an_empty_string():
    tool = ServiceTool(
        name="atl_test",
        description="d",
        parameters={"type": "object", "properties": {}},
        method="echo",
        service=Recorder(result="   "),
    )
    assert run(tool.call(None)) == "（无内容）"
