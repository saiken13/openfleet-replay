"""Password hashing with stdlib scrypt (DECISIONS #022): memory-hard, no extra dependency.

Hashes are self-describing (`scrypt$n$r$p$salt$hash`), so the cost can be raised later
without invalidating stored passwords.
"""

import base64
import hashlib
import hmac
import secrets

# ~50 ms and 16 MiB per hash. Tests lower SCRYPT_N (tests/conftest.py); stored hashes keep
# the parameters they were made with.
SCRYPT_N = 2**14
SCRYPT_R = 8
SCRYPT_P = 1
_DKLEN = 32
MIN_PASSWORD_LENGTH = 8


def _b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).decode().rstrip("=")


def _unb64(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def _scrypt(password: str, salt: bytes, n: int, r: int, p: int) -> bytes:
    return hashlib.scrypt(
        password.encode(), salt=salt, n=n, r=r, p=p, dklen=_DKLEN, maxmem=256 * n * r
    )


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = _scrypt(password, salt, SCRYPT_N, SCRYPT_R, SCRYPT_P)
    return f"scrypt${SCRYPT_N}${SCRYPT_R}${SCRYPT_P}${_b64(salt)}${_b64(digest)}"


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, n, r, p, salt, digest = stored.split("$")
        if scheme != "scrypt":
            return False
        actual = _scrypt(password, _unb64(salt), int(n), int(r), int(p))
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(actual, _unb64(digest))


# Verified against when the username doesn't exist, so a login takes the same time either way.
DUMMY_HASH = hash_password(secrets.token_urlsafe(16))
