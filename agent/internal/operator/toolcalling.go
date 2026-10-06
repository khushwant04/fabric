package operator

import (
	"fmt"
	"strings"
)

// Keep these exact native-template profiles in sync with the control plane's
// app/core/model_profiles.py. Family prefixes are deliberately not used: Qwen3-VL
// emits JSON tool calls, while Qwen3.5 emits XML function/parameter tags.
var modelToolCallParsers = map[string]string{
	"Qwen/Qwen2.5-Coder-3B-Instruct": "hermes",
	"Qwen/Qwen3-VL-4B-Instruct":     "hermes",
	"Qwen/Qwen3.5-4B":              "qwen3_xml",
}

// Built-in names from vLLM v0.26.0. A caller cannot request a plugin to execute
// code or an arbitrary command argument through the deployment specification.
var supportedToolCallParsers = map[string]bool{
	"deepseek_v3": true, "deepseek_v31": true, "deepseek_v32": true, "deepseek_v4": true,
	"cohere_command3": true, "cohere_command4": true, "ernie45": true, "glm45": true, "glm47": true,
	"granite-20b-fc": true, "granite": true, "granite4": true, "hermes": true, "poolside_v1": true,
	"hunyuan_a13b": true, "hy_v3": true, "internlm": true, "jamba": true, "lfm2": true, "kimi_k2": true,
	"llama3_json": true, "llama4_json": true, "llama4_pythonic": true, "longcat": true, "mimo": true,
	"minimax_m2": true, "minimax_m3": true, "minicpm5": true, "mistral": true, "olmo3": true, "openai": true,
	"phi4_mini_json": true, "pythonic": true, "qwen3_coder": true, "qwen3_xml": true, "seed_oss": true,
	"step3": true, "step3p5": true, "inkling": true, "xlam": true, "gigachat3": true, "functiongemma": true,
	"gemma4": true, "apertus": true,
}

func equivalentToolCallParser(parser string) string {
	if parser == "qwen3_coder" {
		return "qwen3_xml"
	}
	return parser
}

// ParseToolCallParsers accepts repeated exact-model=parser installation flags.
// The chart does not impose one parser on every model served by a stamp.
func ParseToolCallParsers(entries []string) (map[string]string, error) {
	parsers := make(map[string]string, len(entries))
	for _, entry := range entries {
		model, parser, found := strings.Cut(entry, "=")
		if !found || model == "" || !supportedToolCallParsers[parser] {
			return nil, fmt.Errorf("tool call parser %q must be exact-model-id=supported-vllm-parser", entry)
		}
		if native := modelToolCallParsers[model]; native != "" && equivalentToolCallParser(parser) != native {
			return nil, fmt.Errorf("model %s requires the %s parser for its native chat template", model, native)
		}
		parsers[model] = parser
	}
	return parsers, nil
}

func (m ModelHost) toolCallingSettings(spec Spec) (bool, string) {
	if spec.EnableAutoToolChoice != nil && !*spec.EnableAutoToolChoice {
		return false, ""
	}
	parser := spec.ToolCallParser
	if parser == "" {
		parser = m.ToolCallParsers[m.ServedName]
		if parser == "" {
			parser = m.ToolCallParsers[m.ModelRef]
		}
		// An explicit enable uses the verified profile even when installation-wide
		// automatic model defaults were disabled. No opinion inherits that policy.
		if parser == "" && (!m.DisableToolCallingModelDefaults || spec.EnableAutoToolChoice != nil) {
			parser = modelToolCallParsers[m.ServedName]
			if parser == "" {
				parser = modelToolCallParsers[m.ModelRef]
			}
		}
	}
	if !supportedToolCallParsers[parser] {
		return false, ""
	}
	return true, parser
}
