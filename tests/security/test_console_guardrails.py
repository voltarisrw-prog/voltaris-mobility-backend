"""The super-admin console is the highest-privilege surface. These tests exist to
prove it cannot be turned into a database shell."""

from __future__ import annotations

import pytest

from app.core.errors import AppError, ErrorCode
from app.infrastructure.database.client import Collections
from app.modules.admin.console import ALWAYS_REDACT, READABLE, _redact, _validate_filter


@pytest.mark.parametrize(
    "dangerous",
    [
        {"$where": "this.price < 1"},  # JavaScript execution
        {"$expr": {"$gt": ["$a", "$b"]}},  # arbitrary expression
        {"name": {"$function": {"body": "function(){}"}}},  # server-side function
        {"$and": [{"$where": "1==1"}]},  # nested inside a legal op
        {"a": {"b": {"c": {"$where": "1"}}}},  # buried deep
    ],
)
def test_execution_operators_are_rejected(dangerous):
    with pytest.raises(AppError) as exc:
        _validate_filter(dangerous)
    assert exc.value.code is ErrorCode.INVALID_REQUEST


def test_ordinary_filters_are_allowed():
    for benign in (
        {"status": "PAID"},
        {"total": {"$gte": 1_000_000}},
        {"$or": [{"status": "PAID"}, {"status": "REFUNDED"}]},
        {"email": {"$regex": "^amani", "$options": "i"}},
    ):
        _validate_filter(benign)


def test_pathological_regex_is_rejected():
    with pytest.raises(AppError):
        _validate_filter({"name": {"$regex": "a" * 300}})
    with pytest.raises(AppError):
        _validate_filter({"name": {"$regex": "([a-z]+"}})


def test_deeply_nested_filters_are_rejected():
    node = {"a": 1}
    for _ in range(10):
        node = {"x": node}
    with pytest.raises(AppError):
        _validate_filter(node)


def test_secrets_are_redacted_even_for_super_admins():
    row = {
        "_id": "u1",
        "email": "a@b.com",
        "password_hash": "$argon2id$v=19$...",
        "mfa_secret": "JBSWY3DP",
        "identities": [{"provider": "google", "subject": "1234"}],
        "roles": ["BUYER"],
    }
    cleaned = _redact(row)
    assert cleaned["password_hash"] == "[redacted]"
    assert cleaned["mfa_secret"] == "[redacted]"
    assert cleaned["identities"] == "[redacted]"
    # Operational data is untouched — the role exists to see everything else.
    assert cleaned["email"] == "a@b.com"
    assert cleaned["roles"] == ["BUYER"]


def test_redaction_list_covers_every_impersonation_vector():
    assert {"password_hash", "mfa_secret", "identities"} <= ALWAYS_REDACT


def test_readable_set_is_an_allow_list_not_a_denylist():
    # A collection added later must be invisible until explicitly listed.
    assert "some_future_collection" not in READABLE
    assert Collections.PAYMENTS in READABLE
