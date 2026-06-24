"""AES-256-CBC + PKCS7 core, byte-for-byte compatible with the original C#
encryption tool this app replaces.

That .NET tool used ``RijndaelManaged`` with:

* a 256-bit key   = the raw UTF-8 bytes of the 32-character key string
* a 128-bit IV    = the raw UTF-8 bytes of the 16-character IV string
* CBC mode, PKCS7 padding
* standard Base64 output (``Convert.ToBase64String``)

No key-derivation and no salt: the key and IV strings are used as raw bytes.
Keeping these exact semantics is what makes a token encrypted by the C# tool
decrypt here (and vice-versa).
"""

import base64

from cryptography.hazmat.primitives import padding as sym_padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

# Hardcoded constants, identical to Form1.cs. Kept verbatim for interoperability.
# (See the security note in the README — do NOT "fix" these in isolation.)
KEY = b"7E892875A52C59A3B588306B13C31FBD"   # 32 bytes -> AES-256
DEFAULT_IV = b"1011121314151617"            # 16 bytes

# AES block size is always 128 bits, regardless of the (256-bit) key size.
_AES_BLOCK_BITS = 128


def _as_key_bytes(key):
    """Coerce a str/bytes key to 32 raw bytes (AES-256), matching .NET's UTF-8 usage."""
    if isinstance(key, str):
        key = key.encode("utf-8")
    if len(key) != 32:
        raise ValueError(f"AES-256 key must be 32 bytes, got {len(key)}")
    return key


def _as_iv_bytes(iv):
    """Coerce a str/bytes IV to 16 raw bytes, matching .NET's UTF-8 usage."""
    if isinstance(iv, str):
        iv = iv.encode("utf-8")
    if len(iv) != 16:
        raise ValueError(f"AES IV must be 16 bytes, got {len(iv)}")
    return iv


def encrypt_data(plaintext, key=KEY, iv=DEFAULT_IV):
    """Encrypt ``plaintext`` (str) and return standard Base64 (str).

    Mirrors ``AESDataProtect.EncryptData(plaintext, key, iv)``.
    """
    key = _as_key_bytes(key)
    iv = _as_iv_bytes(iv)

    padder = sym_padding.PKCS7(_AES_BLOCK_BITS).padder()
    padded = padder.update(plaintext.encode("utf-8")) + padder.finalize()

    encryptor = Cipher(algorithms.AES(key), modes.CBC(iv)).encryptor()
    ciphertext = encryptor.update(padded) + encryptor.finalize()

    return base64.b64encode(ciphertext).decode("ascii")


def decrypt_data(ciphertext_b64, key=KEY, iv=DEFAULT_IV):
    """Decrypt a standard Base64 string and return the plaintext (str).

    Mirrors ``AESDataProtect.DecryptData(ciphertext, key, iv)``.
    """
    key = _as_key_bytes(key)
    iv = _as_iv_bytes(iv)

    ciphertext = base64.b64decode(ciphertext_b64)

    decryptor = Cipher(algorithms.AES(key), modes.CBC(iv)).decryptor()
    padded = decryptor.update(ciphertext) + decryptor.finalize()

    unpadder = sym_padding.PKCS7(_AES_BLOCK_BITS).unpadder()
    plaintext = unpadder.update(padded) + unpadder.finalize()

    return plaintext.decode("utf-8")
