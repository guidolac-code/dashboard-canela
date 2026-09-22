from client_auth import (
    create_session_token,
    hash_password,
    verify_password,
    verify_session_token,
)


def test_password_hash_round_trip():
    encoded = hash_password("a strong client password", salt=b"fixed-test-salt")
    assert verify_password("a strong client password", encoded)
    assert not verify_password("wrong password", encoded)


def test_session_token_is_signed_and_bound_to_username():
    secret = "s" * 48
    token = create_session_token("canela", secret, ttl_seconds=60)
    assert verify_session_token(token, secret, "canela")
    assert not verify_session_token(token, secret, "another-client")
    assert not verify_session_token(token + "x", secret, "canela")
