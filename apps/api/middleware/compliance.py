"""Source-policy compliance enforcement at the API boundary."""

from __future__ import annotations

from collections.abc import Iterable

from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request

from avis.core.quality_engine import CompliancePolicyEngine, CompliancePolicyError, SourcePolicy


DEFAULT_SOURCE_POLICIES = (
    SourcePolicy(
        source_name="NSE_EOD",
        redistribution_allowed=False,
        commercial_use=False,
        attribution_required=True,
    ),
    SourcePolicy(
        source_name="BSE_EOD",
        redistribution_allowed=False,
        commercial_use=False,
        attribution_required=True,
    ),
    SourcePolicy(
        source_name="NSE_CORPORATE_ACTIONS",
        redistribution_allowed=False,
        commercial_use=False,
        attribution_required=True,
    ),
    SourcePolicy(
        source_name="INTERNAL_MODEL",
        redistribution_allowed=True,
        commercial_use=True,
        attribution_required=False,
    ),
)


class ComplianceMiddleware(BaseHTTPMiddleware):
    """Block external responses that violate connector redistribution policy."""

    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)
        checks = getattr(request.state, "compliance_checks", None)
        if not checks or response.status_code >= 400:
            return response

        session_factory: async_sessionmaker[AsyncSession] = request.app.state.session_factory
        async with session_factory() as session:
            try:
                for check in checks:
                    await session.run_sync(
                        lambda sync_session, current=check: _evaluate_check(
                            sync_session,
                            source_policies=getattr(request.app.state, "source_policies", DEFAULT_SOURCE_POLICIES),
                            current=current,
                            external_call=request.headers.get("X-AVIS-Caller", "external").lower() != "internal",
                            user_id=getattr(request.state, "current_user_id", None),
                        )
                    )
                await session.commit()
            except CompliancePolicyError as exc:
                await session.commit()
                return JSONResponse(
                    status_code=403,
                    content={
                        "detail": {
                            "code": "source_policy_blocked",
                            "message": "Response blocked by source redistribution policy",
                            "reason": str(exc),
                            "attribution": check.get("attribution") or f"Source: {check['source_name']}",
                        }
                    },
                )
        return response


def _evaluate_check(
    session,
    *,
    source_policies: Iterable[SourcePolicy],
    current: dict[str, object],
    external_call: bool,
    user_id: int | None,
) -> None:
    engine = CompliancePolicyEngine(session=session)
    for policy in source_policies:
        engine.register_policy(policy)
    engine.evaluate_api_access(
        source_name=str(current["source_name"]),
        external_call=external_call,
        derived_output=bool(current.get("derived_output", True)),
        attribution=current.get("attribution") if isinstance(current.get("attribution"), str) else None,
        user_id=user_id,
        resource_id=str(current["resource_id"]),
    )
