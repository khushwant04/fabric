"""Tool-output formats verified against native model templates and vLLM 0.26.0.

Keep the exact-release defaults in sync with agent/internal/operator/toolcalling.go.
Do not infer a parser from a family prefix: Qwen3-VL emits Hermes JSON, while
Qwen3.5 emits XML function/parameter tags.
"""

TOOL_CALL_PARSERS: dict[str, str] = {
    "Qwen/Qwen2.5-Coder-3B-Instruct": "hermes",
    "Qwen/Qwen3-VL-4B-Instruct": "hermes",
    "Qwen/Qwen3.5-4B": "qwen3_xml",
}


def default_tool_call_parser(release: str) -> str | None:
    return TOOL_CALL_PARSERS.get(release)


def equivalent_tool_call_parser(parser: str) -> str:
    # vLLM 0.26 registers these names against the same Qwen3EngineToolParser.
    return "qwen3_xml" if parser == "qwen3_coder" else parser
