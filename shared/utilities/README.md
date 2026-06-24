# utilities

Small, cross-tool helpers (each their own concern). On `sys.path`, so importable
by name from any tool.

- `decorators.py` — `admin_required` (gate a view to authenticated admins;
  anonymous users are bounced to `auth.login`). Used by the AutoBackupRevert
  admin routes; reusable by any future tool.

Add more single-purpose helpers here; promote anything that grows into a package
to `../common-libraries/`.
