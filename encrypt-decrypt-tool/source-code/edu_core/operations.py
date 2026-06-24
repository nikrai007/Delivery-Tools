"""High-level operations that compose :mod:`crypto` and :mod:`transforms`,
matching the three button handlers in the original C# tool (``Form1.cs``).

The GUI and the CLI both call these, so the behaviour stays in one place.
"""

from .crypto import DEFAULT_IV, decrypt_data, encrypt_data
from .transforms import (
    clean_up_encryption,
    get_custom_iv,
    make_it_dirty_again,
    make_nonce,
)


def encrypt_string(plaintext, custom_iv="", user_id=""):
    """Encrypt button: choose the IV, AES-encrypt, then URL-clean the Base64 output.

    Mirrors ``Form1.encryptString_Click``.
    """
    iv = get_custom_iv(custom_iv, user_id).encode("utf-8")
    return clean_up_encryption(encrypt_data(plaintext, iv=iv))


def decrypt_string(token, custom_iv="", user_id=""):
    """Decrypt button: un-clean the input, then AES-decrypt with the chosen IV.

    Mirrors ``Form1.decryptString_Click``.
    """
    iv = get_custom_iv(custom_iv, user_id).encode("utf-8")
    return decrypt_data(make_it_dirty_again(token), iv=iv)


def generate_nonce():
    """Nonce button: build ``guid:millis``, encrypt with the DEFAULT IV, then URL-clean.

    Mirrors ``Form1.button1_Click`` (which forces the default IV).
    """
    return clean_up_encryption(encrypt_data(make_nonce(), iv=DEFAULT_IV))
