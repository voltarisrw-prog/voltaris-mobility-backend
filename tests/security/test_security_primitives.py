from __future__ import annotations

import time

import pytest

from app.core.errors import AppError, ErrorCode
from app.core.security import (
    create_token,
    decode_token,
    hash_password,
    sign_webhook,
    verify_password,
    verify_webhook_signature,
)


def test_password_hash_is_not_the_password():
    hashed = hash_password("correct-horse-battery")
    assert "correct-horse-battery" not in hashed
    assert hashed.startswith("$argon2id$")


def test_same_password_hashes_differently():
    # Distinct salts. Identical hashes would make the table rainbow-attackable.
    assert hash_password("same-password-x") != hash_password("same-password-x")


def test_verify_rejects_wrong_password_and_garbage_hash():
    hashed = hash_password("correct-horse-battery")
    assert verify_password("correct-horse-battery", hashed)
    assert not verify_password("wrong", hashed)
    assert not verify_password("anything", "not-a-hash")


def test_refresh_token_is_rejected_as_an_access_token():
    token = create_token(subject="u1", token_type="refresh", roles=["BUYER"], session_id="s1")
    with pytest.raises(AppError) as exc:
        decode_token(token, expected_type="access")
    assert exc.value.code is ErrorCode.TOKEN_INVALID


def test_expired_token_is_rejected():
    token = create_token(
        subject="u1", token_type="access", roles=[], session_id="s1", ttl_seconds=-1
    )
    with pytest.raises(AppError) as exc:
        decode_token(token, expected_type="access")
    assert exc.value.code is ErrorCode.TOKEN_EXPIRED


def test_alg_none_token_is_rejected():
    """The classic JWT bypass: strip the signature and claim `alg: none`."""
    import base64
    import json

    def b64(data: dict) -> str:
        return base64.urlsafe_b64encode(json.dumps(data).encode()).rstrip(b"=").decode()

    forged = f"{b64({'alg': 'none', 'typ': 'JWT'})}.{b64({'sub': 'admin', 'typ': 'access'})}."
    with pytest.raises(AppError):
        decode_token(forged, expected_type="access")


def test_tampered_payload_is_rejected():
    token = create_token(subject="u1", token_type="access", roles=["BUYER"], session_id="s1")
    head, payload, signature = token.split(".")
    tampered = f"{head}.{payload[:-2]}XX.{signature}"
    with pytest.raises(AppError):
        decode_token(tampered, expected_type="access")


def test_valid_webhook_signature_passes():
    body = b'{"id":"evt_1"}'
    header = sign_webhook(body, "secret")
    verify_webhook_signature(
        payload=body, signature_header=header, secret="secret", tolerance_seconds=300
    )


def test_webhook_signed_with_the_wrong_secret_is_rejected():
    body = b'{"id":"evt_1"}'
    header = sign_webhook(body, "attacker-secret")
    with pytest.raises(AppError) as exc:
        verify_webhook_signature(
            payload=body, signature_header=header, secret="real-secret", tolerance_seconds=300
        )
    assert exc.value.code is ErrorCode.WEBHOOK_SIGNATURE_INVALID


def test_modified_body_invalidates_the_signature():
    header = sign_webhook(b'{"amount":100}', "secret")
    with pytest.raises(AppError):
        verify_webhook_signature(
            payload=b'{"amount":999999}',
            signature_header=header,
            secret="secret",
            tolerance_seconds=300,
        )


def test_old_webhook_is_rejected_as_a_replay():
    body = b'{"id":"evt_1"}'
    header = sign_webhook(body, "secret", timestamp=int(time.time()) - 4_000)
    with pytest.raises(AppError) as exc:
        verify_webhook_signature(
            payload=body, signature_header=header, secret="secret", tolerance_seconds=300
        )
    assert exc.value.code is ErrorCode.WEBHOOK_SIGNATURE_INVALID


def test_timestamp_cannot_be_swapped_without_resigning():
    """The timestamp is inside the signed material, so refreshing it breaks the MAC."""
    body = b'{"id":"evt_1"}'
    original = sign_webhook(body, "secret", timestamp=int(time.time()) - 4_000)
    digest = original.split("v1=")[1]
    forged = f"t={int(time.time())},v1={digest}"
    with pytest.raises(AppError):
        verify_webhook_signature(
            payload=body, signature_header=forged, secret="secret", tolerance_seconds=300
        )
