# Login (blueprint: `auth`)

Authentication for the whole platform. Every other tool relies on this for
identity.

## Layout

| Path | Role |
|---|---|
| `source-code/auth.py` | Routes: `/login`, `/register`, `/logout`, `/forgot`, `/reset/<token>`. Blueprint `auth`. |
| `authentication-config/login_manager.py` | Flask-Login `LoginManager`, the `User` session model, and the `user_loader`. |
| `templates/` | `login.html`, `register.html`, `forgot.html`, `reset.html` (extend shared `base.html`). |
| `API/` | Reserved for a future programmatic auth API (none today). |

## How it wires in

- `login_manager` is created in `authentication-config/` and `init_app`-ed by the
  factory ([../app.py](../app.py)).
- `auth.py` imports `User` + `login_manager` from `login_manager.py`, and reads
  user records via the shared `models` layer (`database/database-config`).
- On success, login/register redirect to `abr.dashboard`.

## Security notes

- Passwords hashed with Werkzeug PBKDF2 (in `models`).
- Anti-enumeration on `/forgot` (always the same response).
- Password-reset tokens are one-time, hashed, and TTL-bound
  (`RESET_TOKEN_TTL_MINUTES`).

## Known gaps (roadmap)

No CSRF tokens, no login rate-limiting, session cookies don't yet set
`Secure`/`HttpOnly`/`SameSite`. See the AutoBackupRevert overview §10 for the
hardening bundle that covers these.
