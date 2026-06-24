# seed-data

There is no bulk seed dataset. The only bootstrapped row is the **admin user**,
created on first boot by `models.ensure_admin(...)` from these `.env` values:

```
ADMIN_USERNAME=admin
ADMIN_EMAIL=admin@local
ADMIN_PASSWORD=admin     # ← rotate before exposing the app
```

To add seed data for a future tool, drop a `*.sql` file here and load it from a
one-off script, or extend `ensure_admin`-style helpers in
`../database-config/models.py`.
