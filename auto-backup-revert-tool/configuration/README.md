# configuration — AutoBackupRevert

Runtime configuration for this tool comes from the platform `.env`, surfaced via
`shared/constants/constants.py`:

| Key | Meaning | Default |
|---|---|---|
| `MAX_UPLOAD_MB` | Max upload size | `500` |
| `CLEANUP_MINUTES` | Orphaned work-dir cleanup cadence | `60` |
| `JOB_RETENTION_DAYS` | How long generated job dirs are kept | `30` |
| `UPLOAD_ROOT` | Per-job scratch area | `uploads` |

Tool-internal constants (the skip-filter regex `^SBC_ | _LT$ | _LOG$`, allowed
upload extensions, BKP naming) live in `../source-code/core.py` and
`shared/constants/constants.py` (`ALLOWED_UPLOAD_EXT`).
