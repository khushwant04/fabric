"""Native parser compatibility and the public tri-state runtime contract."""

from pathlib import Path

import pytest
from pydantic import ValidationError

from app.schemas import RuntimeSpec


@pytest.mark.parametrize("release,parser", [
    ("Qwen/Qwen3-VL-4B-Instruct", "hermes"),
    ("Qwen/Qwen2.5-Coder-3B-Instruct", "hermes"),
    ("Qwen/Qwen3.5-4B", "qwen3_xml"),
    ("Qwen/Qwen3.5-4B", "qwen3_coder"),
])
def test_explicit_enable_and_native_parser_are_accepted(release, parser):
    assert RuntimeSpec(release=release, enable_auto_tool_choice=True).tool_call_parser is None
    assert RuntimeSpec(release=release, tool_call_parser=parser).tool_call_parser == parser


def test_unknown_model_keeps_defaults_and_can_use_an_explicit_parser():
    runtime = RuntimeSpec(release="vendor/unknown")
    assert runtime.enable_auto_tool_choice is None
    assert runtime.tool_call_parser is None
    runtime = RuntimeSpec(
        release="vendor/tool-model", enable_auto_tool_choice=True, tool_call_parser="hermes"
    )
    assert runtime.tool_call_parser == "hermes"


def test_explicit_disabled_survives_serialization():
    runtime = RuntimeSpec(release="Qwen/Qwen3-VL-4B-Instruct", enable_auto_tool_choice=False)
    assert runtime.model_dump(mode="json", exclude_none=True)["enable_auto_tool_choice"] is False


@pytest.mark.parametrize("settings", [
    {"release": "vendor/unknown", "enable_auto_tool_choice": True},
    {"release": "vendor/unknown", "tool_call_parser": "not_a_vllm_parser"},
    {"release": "vendor/unknown", "tool_call_parser": "hermes --other-flag"},
    {"release": "vendor/unknown", "enable_auto_tool_choice": False, "tool_call_parser": "hermes"},
    {"release": "Qwen/Qwen3-VL-4B-Instruct", "tool_call_parser": "qwen3_xml"},
    {"release": "Qwen/Qwen3.5-4B", "tool_call_parser": "hermes"},
])
def test_invalid_or_incompatible_tool_settings_are_rejected(settings):
    with pytest.raises(ValidationError):
        RuntimeSpec(**settings)


def test_crd_preserves_tool_fields_instead_of_pruning_them():
    # Read the scoped field blocks without executing Helm or importing the application.
    root = Path(__file__).resolve().parents[2]
    crd = (root / "deploy/helm/fabric-stamp/templates/crd.yaml").read_text()
    boolean = crd.split("                enableAutoToolChoice:\n", 1)[1].split(
        "                toolCallParser:\n", 1
    )[0]
    parser = crd.split("                toolCallParser:\n", 1)[1].split(
        "                jwtIssuer:\n", 1
    )[0]
    assert "type: boolean" in boolean
    assert "nullable: true" in boolean
    assert "type: string" in parser
    assert "- hermes" in parser
    assert "- qwen3_xml" in parser
