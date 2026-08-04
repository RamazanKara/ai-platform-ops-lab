"""Model routing and per-sandbox admission policies loaded from YAML manifests."""

from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

import yaml

from app.settings import Settings, validate_sandbox_id

VALID_BACKENDS = {"ollama", "vllm"}


@dataclass(frozen=True)
class ModelRoute:
    """An approved model mapped to a runtime backend with optional aliases.

    ``fallbacks`` is an ordered list of other model ids to try (in order) when this
    route's runtime fails with a retryable/connection error or its circuit is open,
    enabling cross-runtime failover (e.g. vLLM -> Ollama).
    """

    model_id: str
    backend: str
    aliases: tuple[str, ...] = ()
    fallbacks: tuple[str, ...] = ()
    # Progressive delivery: route ``canary_weight`` (0..1) of this model's traffic to
    # ``canary_model_id`` (weighted A/B), and mirror every request to ``shadow_model_id``
    # fire-and-forget (the shadow response is discarded and never affects the caller).
    canary_model_id: str = ""
    canary_weight: float = 0.0
    shadow_model_id: str = ""
    # Characters per token for this model's budget estimate, 0 to use the gateway default.
    # A single global divisor is wrong in opposite directions for different models: prose
    # in a Latin script runs near four characters per token, source code nearer three, and
    # CJK or other non-Latin text close to one. On a platform whose whole point is coding
    # agents, the global default systematically under-estimates the traffic it exists for.
    # Keeping the number per-model in the reviewed catalog makes it calibrated data rather
    # than a constant nobody revisits.
    estimated_chars_per_token: int = 0


@dataclass(frozen=True)
class ModelRoutingPolicy:
    """A set of model routes used to approve and route inference requests."""

    routes: tuple[ModelRoute, ...]
    allow_unlisted: bool = False

    @classmethod
    def default(cls, settings: Settings) -> ModelRoutingPolicy:
        """Build a routing policy from the settings' allowed models or default model."""
        models = settings.allowed_models or (settings.model_id,)
        return cls(
            tuple(ModelRoute(model, settings.runtime_backend) for model in models),
            allow_unlisted=not settings.allowed_models,
        )

    @classmethod
    def from_path(cls, path: Path, settings: Settings) -> ModelRoutingPolicy:
        """Load and validate a ModelRoutingPolicy manifest from the given path."""
        if not path:
            return cls.default(settings)
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError(f"{path} must contain a YAML mapping")
        if data.get("apiVersion") != "platform.ai/v1alpha1":
            raise ValueError("ModelRoutingPolicy apiVersion must be platform.ai/v1alpha1")
        if data.get("kind") != "ModelRoutingPolicy":
            raise ValueError("ModelRoutingPolicy kind must be ModelRoutingPolicy")
        raw_models = data.get("spec", {}).get("models", [])
        if not isinstance(raw_models, list) or not raw_models:
            raise ValueError("ModelRoutingPolicy spec.models must be a non-empty list")
        routes: list[ModelRoute] = []
        seen: set[str] = set()
        for index, item in enumerate(raw_models):
            if not isinstance(item, dict):
                raise ValueError(f"ModelRoutingPolicy spec.models[{index}] must be a mapping")
            model_id = str(item.get("id") or "").strip()
            backend = str(item.get("backend") or settings.runtime_backend).strip().lower()
            aliases = tuple(str(alias).strip() for alias in item.get("aliases", []) if str(alias).strip())
            fallbacks = tuple(str(fb).strip() for fb in item.get("fallbacks", []) if str(fb).strip())
            canary_raw = item.get("canary")
            canary = canary_raw if isinstance(canary_raw, dict) else {}
            canary_model_id = str(canary.get("model") or "").strip()
            canary_weight = float(canary.get("weight", 0.0) or 0.0)
            shadow_raw = item.get("shadow")
            shadow = shadow_raw if isinstance(shadow_raw, dict) else {}
            shadow_model_id = str(shadow.get("model") or "").strip()
            if not model_id:
                raise ValueError(f"ModelRoutingPolicy spec.models[{index}].id is required")
            if backend not in VALID_BACKENDS:
                raise ValueError(f"ModelRoutingPolicy model {model_id} backend must be one of {sorted(VALID_BACKENDS)}")
            if not 0.0 <= canary_weight <= 1.0:
                raise ValueError(f"ModelRoutingPolicy model {model_id} canary.weight must be between 0 and 1")
            chars_per_token = item.get("estimatedCharsPerToken", 0)
            if isinstance(chars_per_token, bool) or not isinstance(chars_per_token, int) or chars_per_token < 0:
                raise ValueError(
                    f"ModelRoutingPolicy model {model_id} estimatedCharsPerToken must be a non-negative integer"
                )
            for name in (model_id, *aliases):
                if name in seen:
                    raise ValueError(f"ModelRoutingPolicy duplicate model or alias: {name}")
                seen.add(name)
            routes.append(
                ModelRoute(
                    model_id=model_id,
                    backend=backend,
                    aliases=aliases,
                    fallbacks=fallbacks,
                    canary_model_id=canary_model_id,
                    canary_weight=canary_weight,
                    shadow_model_id=shadow_model_id,
                    estimated_chars_per_token=chars_per_token,
                )
            )
        return cls(tuple(routes))

    def model_ids(self) -> tuple[str, ...]:
        """Return the canonical model ids of every configured route."""
        return tuple(route.model_id for route in self.routes)

    def resolve(self, requested_model: str | None, default_model: str) -> ModelRoute:
        """Resolve a requested model or default to its route, raising if unapproved."""
        model = requested_model or default_model
        for route in self.routes:
            if model == route.model_id or model in route.aliases:
                return route
        if self.allow_unlisted and requested_model:
            backend = self.routes[0].backend if self.routes else "ollama"
            return ModelRoute(requested_model, backend)
        allowed = ", ".join(self.model_ids())
        raise ValueError(f"model '{model}' is not approved by ModelRoutingPolicy; allowed models: {allowed}")

    def _route_for(self, model: str) -> ModelRoute | None:
        """Return the route whose id or alias matches, or None when unknown."""
        for route in self.routes:
            if model == route.model_id or model in route.aliases:
                return route
        return None

    def resolve_chain(self, requested_model: str | None, default_model: str) -> list[ModelRoute]:
        """Resolve the primary route plus its (deduplicated) fallback routes, in order.

        The primary is resolved as in :meth:`resolve`; each configured fallback id is
        appended when it maps to a known route and is not already in the chain. Unknown
        fallback ids are skipped rather than failing the request.
        """
        primary = self.resolve(requested_model, default_model)
        chain = [primary]
        seen = {primary.model_id}
        for fallback_id in primary.fallbacks:
            route = self._route_for(fallback_id)
            if route is not None and route.model_id not in seen:
                chain.append(route)
                seen.add(route.model_id)
        return chain

    def canary_target(self, route: ModelRoute, roll: float) -> ModelRoute:
        """Return the canary route when ``roll`` falls in the canary weight, else ``route``.

        ``roll`` is a value in [0, 1) (e.g. ``random.random()``); a request is sent to the
        canary model with probability ``route.canary_weight``. Falls back to the primary
        when no canary is configured or the canary id is unknown.
        """
        if not route.canary_model_id or roll >= route.canary_weight:
            return route
        canary = self._route_for(route.canary_model_id)
        return canary if canary is not None else route

    def shadow_target(self, route: ModelRoute) -> ModelRoute | None:
        """Return the route to mirror traffic to fire-and-forget, or None when unset."""
        if not route.shadow_model_id:
            return None
        return self._route_for(route.shadow_model_id)

    def openai_models(self) -> list[dict[str, Any]]:
        """Return the routes formatted as OpenAI ``/v1/models`` list entries."""
        return [
            {
                "id": route.model_id,
                "object": "model",
                "owned_by": "private-ai-platform-kit",
                "permission": [],
            }
            for route in self.routes
        ]


@dataclass(frozen=True)
class SandboxPolicy:
    """Per-sandbox overrides for admission limits and budget allowances."""

    sandbox_id: str
    allowed_models: tuple[str, ...] = ()
    max_messages: int | None = None
    max_prompt_chars: int | None = None
    max_completion_tokens: int | None = None
    allow_streaming: bool | None = None
    request_budget: int | None = None
    prompt_char_budget: int | None = None
    estimated_token_budget: int | None = None


@dataclass(frozen=True)
class SandboxPolicySet:
    """A collection of sandbox policies keyed by sandbox id."""

    policies: dict[str, SandboxPolicy]

    @classmethod
    def empty(cls) -> SandboxPolicySet:
        """Return a policy set with no sandbox overrides."""
        return cls({})

    @classmethod
    def from_path(cls, path: Path | None) -> SandboxPolicySet:
        """Load and validate a SandboxPolicySet manifest from the given path."""
        if not path:
            return cls.empty()
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError(f"{path} must contain a YAML mapping")
        if data.get("apiVersion") != "platform.ai/v1alpha1":
            raise ValueError("SandboxPolicySet apiVersion must be platform.ai/v1alpha1")
        if data.get("kind") != "SandboxPolicySet":
            raise ValueError("SandboxPolicySet kind must be SandboxPolicySet")
        raw_policies = data.get("spec", {}).get("policies", [])
        if not isinstance(raw_policies, list):
            raise ValueError("SandboxPolicySet spec.policies must be a list")
        policies: dict[str, SandboxPolicy] = {}
        for index, item in enumerate(raw_policies):
            if not isinstance(item, dict):
                raise ValueError(f"SandboxPolicySet spec.policies[{index}] must be a mapping")
            sandbox_id = validate_sandbox_id(str(item.get("sandboxId") or ""))
            if sandbox_id in policies:
                raise ValueError(f"SandboxPolicySet duplicate sandboxId: {sandbox_id}")
            budgets = item.get("budgets") or {}
            if not isinstance(budgets, dict):
                raise ValueError(f"SandboxPolicySet policy {sandbox_id} budgets must be a mapping")
            policies[sandbox_id] = SandboxPolicy(
                sandbox_id=sandbox_id,
                allowed_models=tuple(str(model) for model in item.get("allowedModels", []) if str(model)),
                max_messages=_optional_positive_int(item, "maxMessages", sandbox_id),
                max_prompt_chars=_optional_positive_int(item, "maxPromptChars", sandbox_id),
                max_completion_tokens=_optional_positive_int(item, "maxCompletionTokens", sandbox_id),
                allow_streaming=_optional_bool(item, "allowStreaming", sandbox_id),
                request_budget=_optional_non_negative_int(budgets, "requestLimit", sandbox_id),
                prompt_char_budget=_optional_non_negative_int(budgets, "promptCharLimit", sandbox_id),
                estimated_token_budget=_optional_non_negative_int(budgets, "estimatedTokenLimit", sandbox_id),
            )
        return cls(policies)

    def effective_settings(self, settings: Settings, sandbox_id: str) -> Settings:
        """Return settings with the sandbox's policy overrides applied, if any."""
        policy = self.policies.get(sandbox_id)
        if policy is None:
            return settings
        updates: dict[str, Any] = {}
        if policy.allowed_models:
            updates["allowed_models"] = policy.allowed_models
        if policy.max_messages is not None:
            updates["max_messages"] = policy.max_messages
        if policy.max_prompt_chars is not None:
            updates["max_prompt_chars"] = policy.max_prompt_chars
        if policy.max_completion_tokens is not None:
            updates["max_completion_tokens"] = policy.max_completion_tokens
        if policy.allow_streaming is not None:
            updates["allow_streaming"] = policy.allow_streaming
        if policy.request_budget is not None:
            updates["sandbox_request_budget"] = policy.request_budget
        if policy.prompt_char_budget is not None:
            updates["sandbox_prompt_char_budget"] = policy.prompt_char_budget
        if policy.estimated_token_budget is not None:
            updates["sandbox_estimated_token_budget"] = policy.estimated_token_budget
        return replace(settings, **updates) if updates else settings


def _optional_bool(item: dict[str, Any], field: str, sandbox_id: str) -> bool | None:
    if field not in item:
        return None
    value = item[field]
    if not isinstance(value, bool):
        raise ValueError(f"SandboxPolicySet policy {sandbox_id} {field} must be a boolean")
    return value


def _optional_positive_int(item: dict[str, Any], field: str, sandbox_id: str) -> int | None:
    if field not in item:
        return None
    value = item[field]
    if not isinstance(value, int) or value <= 0:
        raise ValueError(f"SandboxPolicySet policy {sandbox_id} {field} must be a positive integer")
    return value


def _optional_non_negative_int(item: dict[str, Any], field: str, sandbox_id: str) -> int | None:
    if field not in item:
        return None
    value = item[field]
    if not isinstance(value, int) or value < 0:
        raise ValueError(f"SandboxPolicySet policy {sandbox_id} budgets.{field} must be zero or greater")
    return value
