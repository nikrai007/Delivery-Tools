# common-libraries

Shared, tool-agnostic libraries. Today the two platform-wide libraries live in
their own structural homes and are imported by name (both their folders are on
`sys.path`):

- **Data access** — `database/database-config/models.py` (`import models`).
- **Email** — `auto-backup-revert-tool/dependencies/email_utils.py`
  (`import email_utils`), currently used by the `auth` password-reset flow.

Put genuinely cross-tool helper packages here as the platform grows (e.g. a
shared pagination helper, a common audit-logging wrapper). Keep
single-responsibility utilities in `../utilities/`.
