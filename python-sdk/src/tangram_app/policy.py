"""Standalone authorization policy interfaces and safe local defaults."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Protocol

from .models import Action, ActionBinding, Effect


@dataclass(frozen=True, slots=True)
class Principal:
    id: str
    kind: str = "local-user"


@dataclass(frozen=True, slots=True)
class InvocationRequest:
    principal: Principal
    action: Action
    binding: ActionBinding
    arguments: Any


class DecisionKind(str, Enum):
    ALLOW = "allow"
    DENY = "deny"
    CONFIRMATION_REQUIRED = "confirmation_required"


@dataclass(frozen=True, slots=True)
class PolicyDecision:
    kind: DecisionKind
    reason: str


class AuthorizationPolicy(Protocol):
    async def authorize(self, request: InvocationRequest) -> PolicyDecision: ...


class LocalDevelopmentPolicy:
    """Read-only by default, with explicit per-action/binding mutation grants."""

    def __init__(
        self,
        *,
        allow_mutations: set[str] | frozenset[str] = frozenset(),
        preauthorized_confirmations: set[str] | frozenset[str] = frozenset(),
    ) -> None:
        self._allow_mutations = frozenset(allow_mutations)
        self._preauthorized_confirmations = frozenset(preauthorized_confirmations)

    @staticmethod
    def _matches(allowed: frozenset[str], request: InvocationRequest) -> bool:
        return request.action.id in allowed or request.binding.id in allowed

    async def authorize(self, request: InvocationRequest) -> PolicyDecision:
        if request.action.effect is not Effect.STATELESS and not self._matches(
            self._allow_mutations, request
        ):
            return PolicyDecision(
                DecisionKind.DENY,
                f"{request.action.effect.value} actions are disabled by local policy",
            )
        if request.action.requires_confirmation and not self._matches(
            self._preauthorized_confirmations, request
        ):
            return PolicyDecision(
                DecisionKind.CONFIRMATION_REQUIRED,
                "the manifest requires confirmation and this call is not pre-authorized",
            )
        return PolicyDecision(DecisionKind.ALLOW, "allowed by local development policy")
