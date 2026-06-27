from __future__ import annotations

from sqlalchemy.dialects.postgresql import INET, JSONB, UUID
from sqlalchemy.ext.compiler import compiles


@compiles(UUID, "sqlite")
def _compile_uuid_sqlite(_type, _compiler, **_kw) -> str:
    return "CHAR(36)"


@compiles(JSONB, "sqlite")
def _compile_jsonb_sqlite(_type, _compiler, **_kw) -> str:
    return "TEXT"


@compiles(INET, "sqlite")
def _compile_inet_sqlite(_type, _compiler, **_kw) -> str:
    return "TEXT"
