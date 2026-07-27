"""Base HTTP client for AVIS API access."""

from __future__ import annotations

import dataclasses
import time
from dataclasses import is_dataclass
from datetime import date, datetime
from decimal import Decimal
from types import UnionType
from typing import Any, TypeVar, get_args, get_origin, get_type_hints
from uuid import UUID

import requests

from avis.client.config import ClientConfig


class CompliancePolicyError(PermissionError):
    """Raised when the API blocks access due to source policy."""

    def __init__(self, message: str, *, attribution: str | None = None) -> None:
        super().__init__(message)
        self.attribution = attribution


class EntityNotFoundError(LookupError):
    """Raised when the requested AVIS entity does not exist."""


class ValidationError(ValueError):
    """Raised when the API rejects request parameters or payloads."""

    def __init__(self, message: str, *, detail: Any = None) -> None:
        super().__init__(message)
        self.detail = detail


T = TypeVar("T")


class AVISClient:
    """Base synchronous client for AVIS FastAPI endpoints."""

    def __init__(
        self,
        base_url: str | None = None,
        jwt_token: str | None = None,
        *,
        session: requests.Session | None = None,
        enabled: bool | None = None,
        timeout: int = 30,
    ) -> None:
        config = ClientConfig.from_env()
        self.base_url = (base_url or config.base_url).rstrip("/")
        self.jwt_token = jwt_token if jwt_token is not None else config.jwt_token
        self.enabled = config.enabled if enabled is None else enabled
        self.timeout = timeout
        self.session = session or requests.Session()
        self.session.headers.setdefault("Accept", "application/json")
        if self.jwt_token:
            self.session.headers["Authorization"] = f"Bearer {self.jwt_token}"

    def _get(self, path: str, *, params: dict[str, Any] | None = None) -> Any | None:
        return self._request("GET", path, params=params)

    def _post(self, path: str, *, json_body: dict[str, Any] | None = None) -> Any | None:
        return self._request("POST", path, json_body=json_body)

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
    ) -> Any | None:
        if not self.enabled:
            return None

        url = f"{self.base_url}{path}"
        attempt = 0
        while True:
            response = self.session.request(
                method,
                url,
                params=params,
                json=json_body,
                timeout=self.timeout,
            )
            if response.status_code not in {500, 503}:
                break
            attempt += 1
            if attempt >= 3:
                break
            time.sleep(0.5 * (2 ** (attempt - 1)))

        if response.status_code >= 400:
            self._raise_for_error(response)
        if response.status_code == 204 or not response.content:
            return None
        return response.json()

    def _raise_for_error(self, response: requests.Response) -> None:
        payload = _safe_json(response)
        detail = payload.get("detail", payload) if isinstance(payload, dict) else payload
        if response.status_code == 403:
            attribution = detail.get("attribution") if isinstance(detail, dict) else None
            message = detail.get("message", "Compliance policy blocked this request") if isinstance(detail, dict) else str(detail)
            raise CompliancePolicyError(message, attribution=attribution)
        if response.status_code == 404:
            message = detail.get("message", "Requested entity was not found") if isinstance(detail, dict) else str(detail)
            raise EntityNotFoundError(message)
        if response.status_code == 422:
            message = detail.get("message", "Request validation failed") if isinstance(detail, dict) else "Request validation failed"
            raise ValidationError(message, detail=detail)
        response.raise_for_status()

    @staticmethod
    def _parse_dataclass(data: Any, model_type: type[T]) -> T:
        return _coerce_value(data, model_type)


def _safe_json(response: requests.Response) -> dict[str, Any] | Any:
    try:
        return response.json()
    except ValueError:
        return {"detail": response.text}


def _coerce_value(value: Any, target_type: Any) -> Any:
    origin = get_origin(target_type)
    if value is None:
        return None
    if origin is list:
        (item_type,) = get_args(target_type)
        return [_coerce_value(item, item_type) for item in value]
    if origin is dict:
        return value
    if origin is tuple:
        item_types = get_args(target_type)
        return tuple(_coerce_value(item, item_type) for item, item_type in zip(value, item_types, strict=False))
    if origin is None and is_dataclass(target_type):
        type_hints = get_type_hints(target_type)
        kwargs = {}
        for field in dataclasses.fields(target_type):
            kwargs[field.name] = _coerce_value(value.get(field.name), type_hints.get(field.name, field.type))
        return target_type(**kwargs)
    if origin is UnionType or str(origin).endswith("Union"):
        args = [arg for arg in get_args(target_type) if arg is not type(None)]
        for arg in args:
            try:
                return _coerce_value(value, arg)
            except Exception:
                continue
        return value
    if target_type is datetime:
        return datetime.fromisoformat(value)
    if target_type is date:
        return date.fromisoformat(value)
    if target_type is Decimal:
        return Decimal(str(value))
    if target_type is UUID:
        return UUID(str(value))
    if target_type in {str, int, bool, float} or target_type is Any:
        return target_type(value) if target_type is not Any and value is not None else value
    return value
