"""Pure ports of the helper logic in the original C# tool (``Form1.cs``):
the URL-safe character swap, the nonce builder, and the IV-derivation helpers.

These functions deliberately do no crypto and no I/O so they stay trivially testable.
"""

import time
import uuid

# (dirty, clean) pairs in the EXACT order used by Form1.CleanUpEncryption.
# IMPORTANT: the placeholders are NOT alphabetical -- '+' maps to '!iiiiii!' and
# ',' maps to '!hhhhhh!'. This mirrors the (off-by-one-looking) arrays in the C# code.
SWAP_PAIRS = [
    (";", "!aaaaaa!"),
    ("/", "!bbbbbb!"),
    ("?", "!cccccc!"),
    (":", "!dddddd!"),
    ("#", "!eeeeee!"),
    ("&", "!ffffff!"),
    ("=", "!gggggg!"),
    ("+", "!iiiiii!"),
    ("$", "!jjjjjj!"),
    (",", "!hhhhhh!"),
]

DEFAULT_IV_STR = "1011121314151617"


def clean_up_encryption(s):
    """Replace URL-unsafe characters with safe placeholders.

    Mirrors ``Form1.CleanUpEncryption``.
    """
    for dirty, clean in SWAP_PAIRS:
        s = s.replace(dirty, clean)
    return s


def make_it_dirty_again(s):
    """Reverse :func:`clean_up_encryption`.

    Mirrors ``Form1.MakeItDirtyAgain``.
    """
    for dirty, clean in SWAP_PAIRS:
        s = s.replace(clean, dirty)
    return s


def unix_time_now_ms():
    """Milliseconds since 1970-01-01 UTC. Mirrors ``Form1.UnixTimeNow``."""
    return time.time_ns() // 1_000_000


def make_nonce():
    """Build ``"<guid>:<unixMillis>"``. Mirrors the nonce string in ``Form1.button1_Click``."""
    return f"{uuid.uuid4()}:{unix_time_now_ms()}"


def get_iv_from_device_id(device_id, user_id):
    """Derive a 16-character IV string from a device id + user id.

    Faithful port of ``Form1.GetIvFromDeviceId(deviceId, userId)``:

    * if the user id already fills (or overflows) 16 chars -> first 16 chars of the user id
    * elif the device id is long enough -> user id + the trailing chars of the device id
    * else -> (user id + device id) right-padded with ``"0"`` up to 16 chars
    """
    required_length = 16 - len(user_id)
    if required_length <= 0:
        return user_id[:16]
    if len(device_id) >= required_length:
        return user_id + device_id[len(device_id) - required_length:]
    return (user_id + device_id).ljust(16, "0")


def get_custom_iv(custom_iv, user_id):
    """Pick the IV string: derived when a custom IV is supplied, else the default.

    Mirrors ``Form1.GetCustomIV`` (note: the "Custom IV" field is fed in as the
    *device id* argument of :func:`get_iv_from_device_id`).
    """
    custom_iv = (custom_iv or "").strip()
    user_id = (user_id or "").strip()
    if custom_iv:
        return get_iv_from_device_id(custom_iv, user_id)
    return DEFAULT_IV_STR
