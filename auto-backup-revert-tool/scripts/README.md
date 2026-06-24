# scripts — AutoBackupRevert

Operational scripts for this tool.

- The platform launcher is `../../run.bat` (starts the whole Delivery Toolbox).
- The **generated** rollback scripts a job produces (`BACKUP.sql`, `REVERT.sql`,
  `CLEANUP.sql`, …) are delivered inside each job's `BUNDLE_*.zip` — they are not
  stored here.
- Sample input migrations for demos live in `../samples/`.

Add maintenance/one-off scripts (e.g. bulk re-processing, manifest repair) here.
