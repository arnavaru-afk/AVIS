"""Generate a stable RS256 development JWT for local AVIS usage."""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal
from uuid import UUID

import jwt

DEV_ANALYST_UUID = UUID("9d2b8ea1-3af7-4d10-9c78-19ab4d119001")
DEV_ANALYST_EMAIL = "dev.analyst@avis.local"
TEST_API_PATH = Path(__file__).resolve().parents[1] / "tests" / "test_api.py"


def _load_test_key(name: Literal["TEST_PRIVATE_KEY", "TEST_PUBLIC_KEY"]) -> str:
    content = TEST_API_PATH.read_text(encoding="utf-8")
    pattern = rf"{name}\s*=\s*\"\"\"(.*?)\"\"\""
    match = re.search(pattern, content, re.DOTALL)
    if match is None:
        raise RuntimeError(f"Unable to locate {name} in {TEST_API_PATH}")
    return match.group(1).strip()


def load_test_private_key() -> str:
    return _load_test_key("TEST_PRIVATE_KEY")


def load_test_public_key() -> str:
    return _load_test_key("TEST_PUBLIC_KEY")


def generate_dev_token(*, user_uuid: UUID = DEV_ANALYST_UUID) -> str:
    now = datetime.now(UTC)
    payload = {
        "sub": str(user_uuid),
        "user_uuid": str(user_uuid),
        "roles": ["ANALYST", "ADMIN"],
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(days=365)).timestamp()),
    }
    return jwt.encode(payload, load_test_private_key(), algorithm="RS256")


def main() -> None:
    print(generate_dev_token())


if __name__ == "__main__":
    main()
