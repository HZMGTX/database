"""Identifiers.

Vault uses UUIDv7 (RFC 9562) rendered as 32 lowercase hex characters with no
dashes.  The first 48 bits are a Unix millisecond timestamp, so identifiers
sort chronologically.  That buys three things worth having:

* ``ORDER BY uid`` approximates creation order without a second index,
* B-tree inserts land at the right-hand edge instead of scattering, and
* a uid carries its own creation time, which makes debugging an export easy.

Within a single millisecond a 12-bit counter keeps issuance monotonic, so two
items created back to back never sort in the wrong order.
"""

import os
import threading
import time

__all__ = [
    "UID_LENGTH",
    "is_uid",
    "short",
    "timestamp_ms",
    "uuid7",
]

UID_LENGTH = 32

# Room to count upward inside one millisecond before having to borrow from the
# next.  Seeding below 2**11 leaves at least 2048 increments of headroom.
_SEQ_BITS = 12
_SEQ_MAX = (1 << _SEQ_BITS) - 1
_SEQ_SEED_MAX = 1 << (_SEQ_BITS - 1)

_lock = threading.Lock()
_last_ms = -1
_last_seq = 0


def _now_ms() -> int:
    return time.time_ns() // 1_000_000


def uuid7(now_ms: "int | None" = None) -> str:
    """Return a fresh UUIDv7 as 32 lowercase hex characters.

    Monotonic: successive calls always return increasing values, even when
    several land in the same millisecond and even if the system clock steps
    backwards.
    """
    global _last_ms, _last_seq

    with _lock:
        ms = _now_ms() if now_ms is None else int(now_ms)

        if ms == _last_ms:
            if _last_seq >= _SEQ_MAX:
                # Counter exhausted inside this millisecond.  Borrow from the
                # next one rather than emit a duplicate; the clock catches up.
                ms = _last_ms + 1
                seq = int.from_bytes(os.urandom(2), "big") % _SEQ_SEED_MAX
            else:
                seq = _last_seq + 1
        elif ms < _last_ms:
            # Clock went backwards (NTP step, VM restore).  Never go with it.
            ms = _last_ms
            if _last_seq >= _SEQ_MAX:
                ms = _last_ms + 1
                seq = int.from_bytes(os.urandom(2), "big") % _SEQ_SEED_MAX
            else:
                seq = _last_seq + 1
        else:
            seq = int.from_bytes(os.urandom(2), "big") % _SEQ_SEED_MAX

        _last_ms, _last_seq = ms, seq

    # 48 bits timestamp | 4 bits version (7) | 12 bits seq
    # | 2 bits variant (0b10) | 62 bits random
    rand_b = int.from_bytes(os.urandom(8), "big") & ((1 << 62) - 1)
    value = (
        ((ms & ((1 << 48) - 1)) << 80)
        | (0x7 << 76)
        | (seq << 64)
        | (0b10 << 62)
        | rand_b
    )
    return f"{value:032x}"


def timestamp_ms(uid: str) -> int:
    """The creation time encoded in *uid*, in Unix milliseconds."""
    if not is_uid(uid):
        raise ValueError(f"not a Vault uid: {uid!r}")
    return int(uid[:12], 16)


def is_uid(value: object) -> bool:
    """True if *value* is exactly 32 lowercase hex characters."""
    if not isinstance(value, str) or len(value) != UID_LENGTH:
        return False
    return all(c in "0123456789abcdef" for c in value)


def short(uid: str, length: int = 8) -> str:
    """The prefix used to refer to an item by hand.

    Eight hex characters is 4 billion values; because the leading bits are a
    timestamp, collisions among items created close together are what matter,
    and the resolver treats an ambiguous prefix as an error rather than
    guessing.
    """
    if length < 4 or length > UID_LENGTH:
        raise ValueError("short id length must be between 4 and 32")
    return uid[:length]
