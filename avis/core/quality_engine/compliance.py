"""Compliance policy enforcement for source usage and redistribution."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from avis.db.models import AuthAuditEvent


class CompliancePolicyError(PermissionError):
    """Raised when a source policy blocks access or publication."""


@dataclass(frozen=True, slots=True)
class SourcePolicy:
    """Connector-level source usage policy."""

    source_name: str
    redistribution_allowed: bool
    commercial_use: bool
    attribution_required: bool


@dataclass(frozen=True, slots=True)
class PolicyDecision:
    """Outcome of a compliance policy evaluation."""

    allowed: bool
    reason: str
    attribution_required: bool
    required_attribution: str | None


class CompliancePolicyEngine:
    """Evaluates connector policies and logs every policy check to audit."""

    def __init__(
        self,
        *,
        session: Session,
        now_provider: Callable[[], datetime] | None = None,
    ) -> None:
        self._session = session
        self._now_provider = now_provider or _utc_now
        self._policies: dict[str, SourcePolicy] = {}

    def register_policy(self, policy: SourcePolicy) -> None:
        self._policies[policy.source_name.upper()] = policy

    def evaluate_api_access(
        self,
        *,
        source_name: str,
        external_call: bool,
        derived_output: bool,
        attribution: str | None,
        user_id: int | None = None,
        commercial_context: bool = False,
        resource_id: str | None = None,
    ) -> PolicyDecision:
        policy = self._policy_for(source_name)
        required_attribution = f"Source: {policy.source_name}" if policy.attribution_required else None

        if commercial_context and not policy.commercial_use:
            decision = PolicyDecision(
                allowed=False,
                reason="commercial_use_blocked",
                attribution_required=policy.attribution_required,
                required_attribution=required_attribution,
            )
            self._log_check(user_id=user_id, policy=policy, resource_id=resource_id, decision=decision)
            raise CompliancePolicyError(decision.reason)

        if external_call and not policy.redistribution_allowed:
            decision = PolicyDecision(
                allowed=False,
                reason="redistribution_blocked",
                attribution_required=policy.attribution_required,
                required_attribution=required_attribution,
            )
            self._log_check(user_id=user_id, policy=policy, resource_id=resource_id, decision=decision)
            raise CompliancePolicyError(decision.reason)

        if derived_output and policy.attribution_required and not attribution:
            decision = PolicyDecision(
                allowed=False,
                reason="attribution_required",
                attribution_required=True,
                required_attribution=required_attribution,
            )
            self._log_check(user_id=user_id, policy=policy, resource_id=resource_id, decision=decision)
            raise CompliancePolicyError(decision.reason)

        decision = PolicyDecision(
            allowed=True,
            reason="allowed",
            attribution_required=policy.attribution_required,
            required_attribution=required_attribution,
        )
        self._log_check(user_id=user_id, policy=policy, resource_id=resource_id, decision=decision)
        return decision

    def _policy_for(self, source_name: str) -> SourcePolicy:
        key = source_name.strip().upper()
        if key not in self._policies:
            raise ValueError(f"No compliance policy registered for {source_name}")
        return self._policies[key]

    def _log_check(
        self,
        *,
        user_id: int | None,
        policy: SourcePolicy,
        resource_id: str | None,
        decision: PolicyDecision,
    ) -> AuthAuditEvent:
        event_type = "COMPLIANCE_POLICY_ALLOW" if decision.allowed else "COMPLIANCE_POLICY_BLOCK"
        audit_event = AuthAuditEvent(
            user_id=user_id,
            event_type=event_type,
            resource_code="SOURCE_POLICY",
            resource_id=resource_id or policy.source_name,
            event_ts=_db_datetime(self._now_provider()),
            ip_address=None,
            user_agent=None,
            event_payload={
                "source_name": policy.source_name,
                "redistribution_allowed": policy.redistribution_allowed,
                "commercial_use": policy.commercial_use,
                "attribution_required": policy.attribution_required,
                "decision": decision.reason,
                "required_attribution": decision.required_attribution,
            },
        )
        _assign_pk_if_sqlite(self._session, audit_event, "audit_event_id", AuthAuditEvent)
        self._session.add(audit_event)
        self._session.flush()
        return audit_event


def _db_datetime(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value
    return value.astimezone(timezone.utc).replace(tzinfo=None)


def _assign_pk_if_sqlite(session: Session, instance: Any, pk_name: str, model: type[Any]) -> None:
    bind = session.get_bind()
    if bind is None or bind.dialect.name != "sqlite":
        return
    if getattr(instance, pk_name, None) is not None:
        return
    pk_column = getattr(model, pk_name)
    current_max = session.scalar(select(func.max(pk_column)))
    setattr(instance, pk_name, 1 if current_max is None else int(current_max) + 1)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)
