# Deployment Guide — Delivery Toolbox

The platform is a single WSGI app (`app:app`). It runs the same on Linux and
Windows; pick a process manager + reverse proxy for your OS.

## 1. Prerequisites

| Component | Why |
|---|---|
| Python 3.11+ | the app |
| `git` binary | the AutoBackupRevert Git connector |
| Visual C++ runtime (Windows) | `py7zr` / `pycryptodomex` wheels |

## 2. First-time setup

```bash
cd Delivery-Tools
python -m venv .venv
. .venv/bin/activate            # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env            # Windows: copy .env.example .env
```

Edit `.env`:

- `FLASK_SECRET_KEY` — set a long random value.
- `ADMIN_USERNAME` / `ADMIN_PASSWORD` — **rotate from the defaults.**
- `PLATFORM_NAME` — defaults to `Delivery Toolbox`.
- `DATA_DIR` — defaults to `database/data` (keep the DB on a **local** disk, not
  SMB/NFS/OneDrive — SQLite locking is only reliable on local filesystems).
- `UPLOAD_ROOT` — per-job working area; defaults to `uploads`.

## 3. Run

**Dev:** `python app.py` → http://127.0.0.1:5000

**Production (single process — required for SQLite):**
```bash
python -m waitress --listen=0.0.0.0:5000 app:app
```
> Run **one** process only. SQLite serialises writes; multiple workers cause
> "database is locked". If you ever need to scale out, migrate to PostgreSQL —
> only the connection layer in `database/database-config/models.py` changes.

## 4. Linux (systemd) — the current deployment

Oracle Cloud Always-Free VM, Ubuntu 22.04, Singapore region. DuckDNS subdomain +
Let's Encrypt. Service runs waitress under `systemd`, nginx terminates TLS and
reverse-proxies to `127.0.0.1:5000`.

```ini
# /etc/systemd/system/delivery-toolbox.service
[Service]
WorkingDirectory=/opt/delivery-toolbox/Delivery-Tools
ExecStart=/opt/delivery-toolbox/Delivery-Tools/.venv/bin/python -m waitress --listen=127.0.0.1:5000 app:app
Restart=always
```

## 5. Windows Server

Use **NSSM** to run waitress as a service, and IIS / Caddy / nginx for TLS:

```powershell
nssm install DeliveryToolbox ^
  "C:\opt\delivery-toolbox\Delivery-Tools\.venv\Scripts\python.exe" ^
  "-m waitress --listen=127.0.0.1:5000 app:app"
nssm set DeliveryToolbox AppDirectory "C:\opt\delivery-toolbox\Delivery-Tools"
nssm set DeliveryToolbox Start SERVICE_AUTO_START
nssm start DeliveryToolbox
```

Run the service as a dedicated low-privilege account with **Modify** on
`database\data\` and `uploads\`. Use Windows-native paths (`C:\releases\inbox`,
`\\fs01\share\...`) in the watched-source admin forms — never UNIX-style paths.

## 6. Reverse proxy / TLS

Bind the public listener to 443 and keep waitress on loopback. nginx (Linux),
IIS + URL Rewrite/ARR, or Caddy (auto Let's Encrypt) all work. The Git connector
needs outbound 443 to your Git host.

## 7. Backups

Back up `database/data/app.db` (the whole platform state) on a schedule. The
`uploads/` dir is transient per-job scratch and need not be backed up.
