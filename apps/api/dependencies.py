"""Dependency injection helpers for the AVIS API."""

from __future__ import annotations

from collections.abc import AsyncGenerator, Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Annotated
from uuid import UUID

from fastapi import Depends, HTTPException, Query, Request, status
from sqlalchemy import Select, and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from apps.api.config import Settings
from avis.db.models import AuthRole, AuthUser, AuthUserRole


@dataclass(frozen=True, slots=True)
class Pagination:
    page: int = 1
    page_size: int = 50

    @property
    def offset(self) -> int:
        return (self.page - 1) * self.page_size


PaginationParams = Annotated[
    Pagination,
    Depends(
        lambda page=Query(1, ge=1), page_size=Query(50, ge=1, le=200): Pagination(
            page=page,
            page_size=page_size,
        )
    ),
]


async def get_db(request: Request) -> AsyncGenerator[AsyncSession, None]:
    session_factory: async_sessionmaker[AsyncSession] = request.app.state.session_factory
    async with session_factory() as session:
        yield session


DbSession = Annotated[AsyncSession, Depends(get_db)]


def get_settings(request: Request) -> Settings:
    return request.app.state.settings


SettingsDep = Annotated[Settings, Depends(get_settings)]


def require_date_window(
    from_date: date | None = Query(default=None),
    to_date: date | None = Query(default=None),
) -> tuple[date | None, date | None]:
    if from_date and to_date and from_date > to_date:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={"code": "invalid_date_range", "message": "from_date must be on or before to_date"},
        )
    return from_date, to_date


DateWindow = Annotated[tuple[date | None, date | None], Depends(require_date_window)]


async def get_current_user(request: Request, db: DbSession) -> AuthUser:
    user_uuid = getattr(request.state, "user_uuid", None)
    if user_uuid is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"code": "auth_required", "message": "Authentication is required"},
        )

    stmt: Select[tuple[AuthUser]] = select(AuthUser).where(
        AuthUser.user_uuid == user_uuid,
        AuthUser.status == "ACTIVE",
    )
    user = await db.scalar(stmt)
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"code": "invalid_user", "message": "Token does not map to an active AVIS user"},
        )
    request.state.current_user_id = user.user_id
    return user


CurrentUser = Annotated[AuthUser, Depends(get_current_user)]


async def _load_role_codes(db: AsyncSession, user_id: int) -> set[str]:
    now = datetime.now(UTC).replace(tzinfo=None)
    stmt = (
        select(AuthRole.role_code)
        .join(AuthUserRole, AuthUserRole.role_id == AuthRole.role_id)
        .where(
            AuthUserRole.user_id == user_id,
            AuthUserRole.valid_from <= now,
            or_(AuthUserRole.valid_to.is_(None), AuthUserRole.valid_to > now),
        )
    )
    return {role_code for role_code in (await db.scalars(stmt)).all()}


async def get_current_role_codes(db: DbSession, current_user: CurrentUser) -> set[str]:
    return await _load_role_codes(db, current_user.user_id)


CurrentRoleCodes = Annotated[set[str], Depends(get_current_role_codes)]


def require_roles(*allowed_roles: str) -> Callable[..., object]:
    allowed = {role.upper() for role in allowed_roles}

    async def dependency(
        db: DbSession,
        current_user: CurrentUser,
    ) -> AuthUser:
        role_codes = await _load_role_codes(db, current_user.user_id)
        if not role_codes.intersection(allowed):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail={
                    "code": "insufficient_role",
                    "message": f"One of {sorted(allowed)} is required for this action",
                },
            )
        return current_user

    return dependency


WriteAuthorizedUser = Annotated[AuthUser, Depends(require_roles("ANALYST", "ADMIN"))]


def register_compliance_check(
    request: Request,
    *,
    source_name: str,
    resource_id: str,
    attribution: str | None = None,
    derived_output: bool = True,
) -> None:
    checks = getattr(request.state, "compliance_checks", None)
    if checks is None:
        checks = []
        request.state.compliance_checks = checks
    checks.append(
        {
            "source_name": source_name,
            "resource_id": resource_id,
            "attribution": attribution,
            "derived_output": derived_output,
        }
    )
