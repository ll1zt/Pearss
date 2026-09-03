"""Reader credential and consumption-state logic.

This is the domain side of the Reader API: it knows what a credential or a
read state *is*, and deliberately knows nothing about Google Reader's stream
ids, item id formats or endpoint shapes. Those stay in
`adapters/reader_api/`, per the boundary rule in docs/api/style-guide.md.

Everything here is pure, so the credential derivation and the state merge can
be tested without a database or an HTTP client.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from dataclasses import dataclass, replace
from datetime import UTC, datetime

# scrypt parameters. Chosen to be memory-hard enough to make offline attacks
# expensive while staying fast enough for a login round-trip on modest
# hardware. Stored alongside the hash so they can be raised later.
SCRYPT_N = 2**14  # 16384
SCRYPT_R = 8
SCRYPT_P = 1
SCRYPT_DKLEN = 32
SCRYPT_SALT_BYTES = 16

# Prefix marking a scrypt hash, so a future algorithm can be distinguished
# without guessing from the shape.
_HASH_SCHEME = "scrypt"


@dataclass(frozen=True, slots=True, kw_only=True)
class ReadState:
    """Consumption state for one item.

    A missing row means unread and unstarred; this object exists once a client
    has reported something. Separate from Item because consumption state is
    mutable and client-owned, whereas a published Item's content is frozen.
    """

    item_id: str
    is_read: bool = False
    is_starred: bool = False
    updated_at: datetime | None = None

    def __post_init__(self) -> None:
        if not self.item_id:
            raise ValueError("ReadState.item_id must not be empty")
        if self.updated_at is not None and self.updated_at.tzinfo is None:
            raise ValueError("updated_at must be timezone-aware")


@dataclass(frozen=True, slots=True, kw_only=True)
class TagChange:
    """A single add/remove of a consumption tag.

    Expressed in domain terms. The mapping from Google Reader's
    `user/-/state/com.google/read` strings onto these flags happens in the
    adapter, never here.
    """

    is_read: bool | None = None
    is_starred: bool | None = None


def hash_password(password: str, *, salt: bytes | None = None) -> str:
    """Derive a scrypt hash, self-describing in its output.

    Format: `scrypt$<n>$<r>$<p>$<salt-hex>$<hash-hex>`. The parameters are
    embedded so a future migration can raise the cost without having to guess
    what an existing row used.

    Uses stdlib scrypt rather than bcrypt or argon2 deliberately: the project
    already carries one C extension (lxml), and a single-user login does not
    justify a second. See ADR 0005.
    """
    if not password:
        raise ValueError("password must not be empty")
    chosen_salt = salt if salt is not None else secrets.token_bytes(SCRYPT_SALT_BYTES)
    digest = hashlib.scrypt(
        password.encode("utf-8"),
        salt=chosen_salt,
        n=SCRYPT_N,
        r=SCRYPT_R,
        p=SCRYPT_P,
        dklen=SCRYPT_DKLEN,
    )
    return "$".join(
        [
            _HASH_SCHEME,
            str(SCRYPT_N),
            str(SCRYPT_R),
            str(SCRYPT_P),
            chosen_salt.hex(),
            digest.hex(),
        ]
    )


def verify_password(password: str, stored: str) -> bool:
    """Check `password` against a stored hash, in constant time.

    Malformed or unknown-scheme hashes return False rather than raising: a
    corrupt credential row should fail the login, not crash the request.
    """
    if not password or not stored:
        return False
    parts = stored.split("$")
    if len(parts) != 6 or parts[0] != _HASH_SCHEME:
        return False
    try:
        cost_n, cost_r, cost_p = int(parts[1]), int(parts[2]), int(parts[3])
        salt = bytes.fromhex(parts[4])
        expected = bytes.fromhex(parts[5])
    except ValueError:
        return False

    candidate = hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=cost_n,
        r=cost_r,
        p=cost_p,
        dklen=len(expected),
    )
    return secrets.compare_digest(candidate, expected)


def derive_auth_token(*, username: str, password_hash: str) -> str:
    """Derive the Reader API auth token.

    HMAC-SHA256 over an empty message, keyed with username + stored hash. The
    stored hash is server-only, so the token cannot be forged from the
    password alone.

    Because the hash is an input, rotating the password issues a new token and
    invalidates the old one — which is why changing the password signs every
    logged-in client out. Recorded in ADR 0005.
    """
    if not username:
        raise ValueError("username is required")
    if not password_hash:
        raise ValueError("password_hash is required")
    key = f"{username}:{password_hash}".encode()
    return hmac.new(key, b"", hashlib.sha256).hexdigest()


def merge_state(
    *, item_id: str, current: ReadState | None, change: TagChange, at: datetime
) -> ReadState:
    """Apply `change` to `item_id`'s state, creating the row if absent."""
    if not item_id:
        raise ValueError("item_id is required")
    if at.tzinfo is None:
        raise ValueError("at must be timezone-aware")

    is_read = change.is_read
    is_starred = change.is_starred
    if current is None:
        return ReadState(
            item_id=item_id,
            is_read=bool(is_read),
            is_starred=bool(is_starred),
            updated_at=at.astimezone(UTC),
        )
    return replace(
        current,
        is_read=current.is_read if is_read is None else is_read,
        is_starred=current.is_starred if is_starred is None else is_starred,
        updated_at=at.astimezone(UTC),
    )
