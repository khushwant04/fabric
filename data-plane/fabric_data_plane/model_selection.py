"""Deterministic, stamp-local model selection for ``model=auto``.

The selector deliberately uses bounded structural inspection and small heuristics. It
never sends prompts to a classifier service, logs prompt text, or consults the control
plane on the inference path. Deployment metadata remains the source of truth; heuristics
only choose among compatible deployments owned by the authenticated account.
"""

from __future__ import annotations

import dataclasses
import re
from collections.abc import Iterable
from typing import Any
from urllib.parse import quote

from fabric_data_plane.registry import Deployment

AUTO_MODEL = "auto"
ROUTING_POLICY = "heuristic-v1"
TASKS = frozenset({"general", "code", "reasoning"})
_MAX_INSPECTED_CHARS = 200_000

_CODE_PATTERN = re.compile(
    r"```|\b(?:code|function|class|method|api|sql|regex|compile|debug|exception|"
    r"traceback|javascript|typescript|python|golang|rust|java|c\+\+|html|css)\b",
    re.IGNORECASE,
)
_REASONING_PATTERN = re.compile(
    r"\b(?:reason|reasoning|analy[sz]e|step[ -]by[ -]step|prove|derive|calculate|"
    r"solve|evaluate|compare|trade-?offs?|why)\b",
    re.IGNORECASE,
)


@dataclasses.dataclass(frozen=True)
class RoutingProfile:
    operation: str
    task: str = "general"
    requires_vision: bool = False
    estimated_tokens: int = 0
    explain: bool = False


def _request_text(payload: dict[str, Any]) -> tuple[str, bool]:
    """Extract bounded user-visible text and whether chat content includes an image."""
    fragments: list[str] = []
    remaining = _MAX_INSPECTED_CHARS
    requires_vision = False

    def add(value: Any) -> None:
        nonlocal remaining, requires_vision
        if isinstance(value, str):
            if remaining <= 0:
                return
            fragments.append(value[:remaining])
            remaining -= min(len(value), remaining)
            return
        if isinstance(value, list):
            for item in value:
                add(item)
            return
        if not isinstance(value, dict):
            return

        kind = value.get("type")
        if kind in {"image_url", "input_image", "image"}:
            requires_vision = True
        # Text classification is bounded, but structural capability checks must still
        # inspect subsequent content parts. A long text prefix cannot make a later image
        # disappear and route a multimodal request to a text-only model host.
        # Only inspect fields that can contain prompt text. URLs and binary-like values
        # are intentionally excluded from both classification and token estimates.
        for name in ("text", "content", "prompt"):
            if name in value:
                add(value[name])

    if "messages" in payload:
        add(payload["messages"])
    elif "prompt" in payload:
        add(payload["prompt"])
    return "\n".join(fragments), requires_vision


def profile_for_json(operation: str, payload: dict[str, Any]) -> RoutingProfile:
    """Classify an OpenAI-compatible JSON request without retaining its content."""
    task_hint: str | None = None
    explain = False
    if "routing" in payload:
        routing = payload["routing"]
        if not isinstance(routing, dict):
            raise ValueError("routing must be an object")
        unknown = set(routing) - {"task", "explain"}
        if unknown:
            raise ValueError(f"unknown routing fields: {', '.join(sorted(unknown))}")
        if "task" in routing:
            raw_task = routing["task"]
            if not isinstance(raw_task, str) or raw_task not in TASKS:
                raise ValueError("routing.task must be one of: general, code, reasoning")
            task_hint = raw_task
        if "explain" in routing:
            if not isinstance(routing["explain"], bool):
                raise ValueError("routing.explain must be a boolean")
            explain = routing["explain"]

    text, requires_vision = _request_text(payload)
    if task_hint is not None:
        task = task_hint
    elif _CODE_PATTERN.search(text):
        task = "code"
    elif _REASONING_PATTERN.search(text):
        task = "reasoning"
    else:
        task = "general"

    # Four characters per token is intentionally approximate. A known context limit is
    # only used to rule out obviously incompatible deployments; the model host remains
    # authoritative and tokenizes the actual request.
    estimated_tokens = (len(text) + 3) // 4
    requested_output = payload.get("max_completion_tokens", payload.get("max_tokens", 0))
    if isinstance(requested_output, int) and not isinstance(requested_output, bool):
        estimated_tokens += max(0, requested_output)

    return RoutingProfile(
        operation=operation,
        task=task,
        requires_vision=requires_vision,
        estimated_tokens=estimated_tokens,
        explain=explain,
    )


def explanation_headers(
    deployment: Deployment, profile: RoutingProfile, *, pinned: bool
) -> dict[str, str]:
    """Return opt-in, bounded diagnostics without prompts or private backend metadata."""
    if not profile.explain:
        return {}
    if pinned:
        reason = "exact_model_pinned"
    elif profile.requires_vision:
        reason = "auto_capability_fit"
    elif (profile.task == "code" and deployment.capabilities.code) or (
        profile.task == "reasoning" and deployment.capabilities.reasoning
    ):
        reason = "auto_task_fit"
    elif (
        profile.task == "general"
        and not deployment.capabilities.code
        and not deployment.capabilities.reasoning
    ):
        reason = "auto_general_fit"
    else:
        reason = "auto_capability_fit"
    # Aliases are public, but local configuration can predate API validation. Percent
    # encoding keeps arbitrary Unicode/control characters out of HTTP header values;
    # the source-length bound also handles a malformed, oversized local alias safely.
    selected_model = quote(deployment.model_alias[:200], safe="-._~")
    return {
        "X-Fabric-Selected-Model": selected_model,
        "X-Fabric-Routing-Task": profile.task,
        "X-Fabric-Routing-Reason": reason,
        "X-Fabric-Routing-Policy": ROUTING_POLICY,
    }


def profile_for_audio(operation: str) -> RoutingProfile:
    return RoutingProfile(operation=operation)


def _compatible(deployment: Deployment, profile: RoutingProfile) -> bool:
    capabilities = deployment.capabilities
    if not capabilities.auto_enabled or not capabilities.supports(profile.operation):
        return False
    if profile.requires_vision and not capabilities.vision:
        return False
    if (
        deployment.max_model_len is not None
        and profile.estimated_tokens > deployment.max_model_len
    ):
        return False
    return True


def _task_affinity(deployment: Deployment, task: str) -> int:
    capabilities = deployment.capabilities
    if task == "code":
        if capabilities.code:
            return 2
        return 1 if not capabilities.reasoning else 0
    if task == "reasoning":
        if capabilities.reasoning:
            return 2
        return 1 if not capabilities.code else 0
    # General prompts prefer a general deployment over consuming a specialist when
    # priorities are equal within the same affinity tier.
    return 1 if not capabilities.code and not capabilities.reasoning else 0


def rank_deployments(
    deployments: Iterable[Deployment], profile: RoutingProfile
) -> list[Deployment]:
    """Return compatible deployments in deterministic best-fit order."""
    compatible = [item for item in deployments if _compatible(item, profile)]
    return sorted(
        compatible,
        key=lambda item: (
            -_task_affinity(item, profile.task),
            -item.capabilities.priority,
            item.model_alias,
            str(item.deployment_id),
        ),
    )
