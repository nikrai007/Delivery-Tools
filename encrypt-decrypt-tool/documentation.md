# Encrypt Decrypt Utility (blueprint: `edu`)

AES-256-CBC encrypt / decrypt of strings and encrypted nonces, with URL-safe
tokens. **Byte-for-byte interoperable** with the original C#/desktop tool — a
token produced by one decrypts in the other.

## Layout

| Path | Role |
|---|---|
| `source-code/edu_core/` | The crypto core, ported **verbatim** from the desktop tool: `crypto.py` (AES-256-CBC + PKCS7), `transforms.py` (URL-safe char-swap, IV derivation, nonce), `operations.py` (the three button flows). |
| `source-code/edu_routes.py` | Flask blueprint `edu`. `GET /tools/encrypt-decrypt/` (page) + `POST /tools/encrypt-decrypt/api` (JSON). |
| `templates/encrypt_decrypt.html` | The UI (extends `templates/tool_base.html`). |

## How it works

The crypto runs **server-side** in Python — the same tested module the desktop
app uses — so the AES key never reaches the browser (more secure than the
original desktop tool, where the key lived in-process on the client). The page's
JS is the UI only; it POSTs `{action, text, custom_iv, user_id}` to `/api` and
renders `{ok, value}` / `{ok, error}` (the exact contract of the desktop
pywebview bridge, including the failed-decrypt hint
`(check the token / IV / User Id)`).

## Crypto (frozen constants — do not change)

- **AES-256-CBC, PKCS7, standard Base64.** KEY = raw UTF-8 bytes of the 32-char
  string `7E892875A52C59A3B588306B13C31FBD`; DEFAULT_IV = raw bytes of
  `1011121314151617`. No key derivation, no salt.
- **URL-safe char-swap** (applied after Base64; reversed before decode) in the
  exact order in `transforms.SWAP_PAIRS` — note `+` → `!iiiiii!` and
  `,` → `!hhhhhh!` (intentionally not alphabetical).
- **IV selection** from Custom IV + User Id via `get_custom_iv` /
  `get_iv_from_device_id` (the Custom IV field is fed in as the *device id*).
- **Nonce** = `"<uuid>:<unix_millis>"` encrypted with the **default** IV.

## Features

Encrypt · Decrypt · Nonce (custom IV / User Id honored for encrypt+decrypt,
ignored for nonce); status line (`IDLE → ENCRYPTING/ENCRYPTED`, …, red on
error); `⚠ message` in the output on error; toast; Copy (with execCommand
fallback) / Clear; keyboard `Ctrl+Enter` = Encrypt, `Ctrl+Shift+Enter` = Decrypt;
action buttons disabled during an operation.

## Acceptance vector

Encrypting `hello world` (default IV) →
**`iKjHMIWO2oqggDlHzYUnDw!gggggg!!gggggg!`**; decrypting it returns `hello world`.
Verified server-side against the ported module.
