import pytest
from fastapi import HTTPException

import auth


def test_password_hash_roundtrip():
    h = auth.hash_password("correct horse battery")
    assert h.startswith("$argon2id$")
    assert auth.verify_password(h, "correct horse battery")
    assert not auth.verify_password(h, "wrong password!!")


def test_unknown_user_never_verifies():
    # stored=None must fail even if the caller guesses the dummy password
    assert not auth.verify_password(None, "not-a-real-password")


@pytest.mark.parametrize("name,ok", [("admin", True), ("a.b_c-1", True), ("ab", False), ("Alice", False), ("a b c", False), ("x" * 33, False)])
def test_username_rules(name, ok):
    assert bool(auth.USERNAME_RE.match(name)) is ok


def test_password_strength():
    auth.check_password_strength("long-enough-pw")
    with pytest.raises(HTTPException):
        auth.check_password_strength("short")


def test_rate_limiter_blocks_after_limit():
    rl = auth.RateLimiter(3, 60)
    for _ in range(3):
        rl.check("k")
    with pytest.raises(HTTPException) as e:
        rl.check("k")
    assert e.value.status_code == 429
    rl.check("other-key")  # keys are independent


def test_temp_passwords_are_unique_and_valid():
    a, b = auth.temp_password(), auth.temp_password()
    assert a != b and len(a) >= auth.MIN_PASSWORD
