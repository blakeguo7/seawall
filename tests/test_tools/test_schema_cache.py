"""Tool schemas are generated once per input model, and callers cannot spoil the copy."""

from __future__ import annotations

from pydantic import BaseModel

from seawall.tools import create_default_tool_registry
from seawall.tools.base import input_schema


class Params(BaseModel):
    path: str
    limit: int = 5


def test_the_cached_schema_equals_a_fresh_one() -> None:
    assert input_schema(Params) == Params.model_json_schema()
    assert input_schema(Params) == input_schema(Params)


def test_changing_a_returned_schema_does_not_change_the_next() -> None:
    first = input_schema(Params)
    first["properties"]["path"]["description"] = "tampered"
    first["required"].append("extra")
    again = input_schema(Params)
    assert "description" not in again["properties"]["path"]
    assert again["required"] == ["path"]


def test_every_default_tool_still_produces_its_schema() -> None:
    for tool in create_default_tool_registry().list_tools():
        schema = tool.to_api_schema()
        assert schema["name"] == tool.name
        assert schema["input_schema"] == tool.input_model.model_json_schema()
