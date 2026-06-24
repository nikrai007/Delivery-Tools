"""
Delivery Toolbox - deployment doctor.

Run this ON the server (from the Delivery-Tools folder) to pinpoint a broken
deployment (the usual cause of a 500 on /login or a missing logo):

    python check_deploy.py

It checks every critical file/folder, imports the app, and renders the public
pages with tracebacks shown - so the exact failure is printed, not hidden
behind the generic "Internal Server Error" page.
"""
import os
import sys
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

ok = True


def check(label, cond, hint=""):
    global ok
    mark = "OK  " if cond else "FAIL"
    if not cond:
        ok = False
    print(f"  [{mark}] {label}" + (f"   -> {hint}" if (hint and not cond) else ""))
    return cond


print("=== Delivery Toolbox deployment check ===")
print("ROOT:", ROOT)

print("\n1. Shared chrome + static (missing these => 500 on every page / no logo):")
check("templates/base.html", (ROOT / "templates" / "base.html").exists(),
      "shared chrome folder not deployed -> TemplateNotFound -> 500")
check("templates/_flash.html", (ROOT / "templates" / "_flash.html").exists())
check("templates/_status_badge.html", (ROOT / "templates" / "_status_badge.html").exists())
for asset in ["logo.svg", "favicon.svg", "style.css", "app.js"]:
    check(f"static/{asset}", (ROOT / "static" / asset).exists(),
          "static/ folder not deployed -> missing logo/styles")

print("\n2. Per-tool blueprint templates:")
check("login/templates/login.html", (ROOT / "login" / "templates" / "login.html").exists())
check("landing-page/templates/landing.html", (ROOT / "landing-page" / "templates" / "landing.html").exists())
check("auto-backup-revert-tool/templates/dashboard.html",
      (ROOT / "auto-backup-revert-tool" / "templates" / "dashboard.html").exists())

print("\n3. Code dirs on sys.path (missing => ModuleNotFoundError at startup):")
for rel in ["shared/constants", "shared/utilities", "database/database-config",
            "login/authentication-config", "login/source-code", "landing-page/source-code",
            "auto-backup-revert-tool/dependencies", "auto-backup-revert-tool/source-code"]:
    check(rel, (ROOT / rel).is_dir())

print("\n4. Import the application factory:")
try:
    import app as a  # noqa
    check("import app (factory boots)", True)
    check("4 blueprints registered", len(a.app.blueprints) >= 4,
          f"only {len(a.app.blueprints)}: {sorted(a.app.blueprints)}")
    import constants  # noqa
    check("database reachable", os.path.exists(constants.DB_PATH),
          f"DB not found at {constants.DB_PATH}")
except Exception:
    check("import app (factory boots)", False)
    print("\n   IMPORT TRACEBACK:\n" + traceback.format_exc())
    print("RESULT: FAIL (app does not import) -- fix the above before serving.")
    sys.exit(1)

print("\n5. Render public pages (shows the real error behind a 500):")
a.app.testing = True  # propagate exceptions instead of generic 500
client = a.app.test_client()
for path in ["/", "/login", "/about", "/register", "/forgot"]:
    try:
        r = client.get(path)
        check(f"GET {path} -> {r.status_code}", r.status_code == 200,
              f"status {r.status_code}")
    except Exception:
        check(f"GET {path}", False)
        print("\n   RENDER TRACEBACK for " + path + ":\n" + traceback.format_exc())

print("\n=== RESULT:", "ALL OK - deployment looks healthy." if ok else "PROBLEMS FOUND (see FAIL lines above).", "===")
sys.exit(0 if ok else 1)
