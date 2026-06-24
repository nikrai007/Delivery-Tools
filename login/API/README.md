# API — login

Reserved for a future **programmatic authentication API** (e.g. token issuance
for CI/headless clients).

There is no auth API today — the platform uses session cookies via Flask-Login
(see `../authentication-config/login_manager.py`). A prior REST API surface was
deliberately removed to reduce attack surface. When an API is added, put its
blueprint/handlers here and register it in `../../app.py`.
