from __future__ import annotations

import sys
from pathlib import Path
from uuid import UUID

import jwt

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.dev_token import DEV_ANALYST_UUID, generate_dev_token, load_test_public_key


def test_dev_token_is_valid_rs256_and_contains_expected_claims() -> None:
    token = generate_dev_token()
    payload = jwt.decode(token, load_test_public_key(), algorithms=["RS256"])

    assert UUID(str(payload["user_uuid"])) == DEV_ANALYST_UUID
    assert payload["roles"] == ["ANALYST", "ADMIN"]
    assert payload["exp"] > payload["iat"]
