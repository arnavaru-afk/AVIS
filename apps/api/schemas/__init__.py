"""Pydantic schemas shared across API routers."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from uuid import UUID

from pydantic import BaseModel, ConfigDict


class AvisSchema(BaseModel):
    model_config = ConfigDict(
        from_attributes=True,
        populate_by_name=True,
        json_encoders={
            Decimal: lambda value: str(value),
            UUID: str,
            datetime: lambda value: value.isoformat(),
            date: lambda value: value.isoformat(),
        },
    )
