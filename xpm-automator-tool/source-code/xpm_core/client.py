"""
XPMClient — the HTTP conversation with the XPM CRM (ASP.NET WebForms).

A faithful, hardened port of the proven desktop flow (``xpm_gui.py`` /
``xpm_uploader.py``) with the shortcomings removed:

  * value object config instead of module globals
  * structured results & typed :class:`XPMError` instead of ``sys.exit``
  * transport-level **retry with backoff** on transient network errors
  * a pluggable ``log`` callback so the web layer streams progress live
  * BeautifulSoup replaced by the stdlib :mod:`xpm_core.html_forms` parser
  * explicit, separate timeouts for short ops / uploads / downloads

No Flask, no DB — a pure, unit-testable service object.
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass
from typing import Callable, Optional

import requests

from .config import USER_AGENT, XPMConfig
from .html_forms import FormFields, parse_batch_rows, parse_select_options

LogFn = Callable[[str, int], None]


class XPMError(RuntimeError):
    """Raised for any XPM interaction failure with an operator-friendly message."""


@dataclass
class UploadOutcome:
    filename: str
    ok: bool
    error: Optional[str] = None
    attempts: int = 1


@dataclass
class BatchScript:
    batch_no: int
    script_name: str
    scripted_by: str
    scripted_on: str
    url: str
    process: str = ""   # the "Process" column (NoteBookTable cell[1])

    def as_dict(self) -> dict:
        return {
            "batch_no": self.batch_no, "script_name": self.script_name,
            "scripted_by": self.scripted_by, "scripted_on": self.scripted_on,
            "url": self.url, "process": self.process,
        }


class XPMClient:
    def __init__(self, cfg: XPMConfig, log: Optional[LogFn] = None,
                 should_cancel: Optional[Callable[[], bool]] = None) -> None:
        self.cfg = cfg
        self._log = log or (lambda msg, level=logging.INFO: None)
        self._should_cancel = should_cancel or (lambda: False)
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": USER_AGENT})
        self.session.verify = cfg.verify_tls

    # -- logging -------------------------------------------------------------
    def log(self, msg: str, level: int = logging.INFO) -> None:
        self._log(msg, level)

    def _check_cancel(self) -> None:
        if self._should_cancel():
            raise XPMError("Run cancelled by user.")

    # -- transport with retry/backoff ---------------------------------------
    def _request(self, method: str, url: str, *, timeout: int, **kwargs) -> requests.Response:
        """One HTTP call with bounded retries on transient transport errors.
        HTTP status is left to the caller — only connection/timeout faults retry."""
        attempts = self.cfg.max_retries + 1
        last_exc: Optional[Exception] = None
        for i in range(1, attempts + 1):
            self._check_cancel()
            try:
                return self.session.request(method, url, timeout=timeout, **kwargs)
            except (requests.exceptions.ConnectionError,
                    requests.exceptions.Timeout) as exc:
                last_exc = exc
                if i < attempts:
                    backoff = min(8, 2 ** (i - 1))
                    self.log(f"Transient network error ({exc.__class__.__name__}); "
                             f"retry {i}/{attempts - 1} in {backoff}s…", logging.WARNING)
                    time.sleep(backoff)
                else:
                    break
        raise XPMError(
            "Could not reach XPM — is the Noida office VPN connected? "
            f"Last error: {last_exc}"
        )

    def _get(self, url: str, *, timeout: Optional[int] = None, **kw) -> requests.Response:
        return self._request("GET", url, timeout=timeout or self.cfg.timeout, **kw)

    def _post(self, url: str, *, timeout: Optional[int] = None, **kw) -> requests.Response:
        return self._request("POST", url, timeout=timeout or self.cfg.timeout, **kw)

    @staticmethod
    def _abs_url(base: str, href: str) -> str:
        from urllib.parse import urljoin, urlparse
        if href.startswith("http"):
            return href
        if href.startswith("/"):
            p = urlparse(base)
            return f"{p.scheme}://{p.netloc}{href}"
        return urljoin(base.rstrip("/") + "/", href)

    # -- steps ---------------------------------------------------------------
    def login(self) -> None:
        url = self.cfg.url("login")
        self.log("Fetching login page…")
        r = self._get(url)
        r.raise_for_status()
        form = FormFields.parse(r.text)

        payload = {
            "__VIEWSTATE": form.value("__VIEWSTATE"),
            "__VIEWSTATEGENERATOR": form.value("__VIEWSTATEGENERATOR"),
            "__EVENTVALIDATION": form.value("__EVENTVALIDATION"),
            "userIdText": self.cfg.username,
            "passwordText": self.cfg.password,
            "Submit.x": "1", "Submit.y": "1",
        }
        self.log(f"Submitting credentials for {self.cfg.username}…")
        r = self._post(url, data=payload, allow_redirects=True)
        r.raise_for_status()
        if "login.aspx" in r.url.lower():
            raise XPMError("Login failed — still on the login page. Check the username / password.")
        self.log("Login successful.")

    def select_project(self) -> None:
        self.log(f"Switching to project {self.cfg.project_id} ({self.cfg.project_name})…")
        r = self._get(self.cfg.url("change_project"),
                      params={"prj": self.cfg.project_id}, allow_redirects=True)
        r.raise_for_status()
        self.log("Project switch complete.")

    def upload_file(self, file_path: str) -> bool:
        """Upload one migration script. Returns True on the XPM success signal."""
        filename = os.path.basename(file_path)
        url = self.cfg.url("new_script")
        self.log(f"Opening upload form for {filename}…")

        r = self._get(url)
        r.raise_for_status()
        form = FormFields.parse(r.text)

        # Resolve the process dropdown value by its visible text; fall back to
        # whatever is pre-selected (byte-compatible with the desktop tool).
        process_value = form.value("cmbProcess")
        if form.has_select("cmbProcess"):
            matched = form.option_value_by_text("cmbProcess", self.cfg.process_name)
            if matched is not None:
                process_value = matched
            else:
                self.log(f"Process '{self.cfg.process_name}' not found in the dropdown; "
                         f"using the pre-selected value.", logging.WARNING)

        data = {
            "__VIEWSTATE": form.value("__VIEWSTATE"),
            "__VIEWSTATEGENERATOR": form.value("__VIEWSTATEGENERATOR"),
            "__EVENTTARGET": "", "__EVENTARGUMENT": "",
            "cmbProcess": process_value,
            "lcObject$lookUpDropDownList": form.value("lcObject$lookUpDropDownList", "6"),
            "lcCategory$lookUpDropDownList": form.value("lcCategory$lookUpDropDownList", "1"),
            "lcStatus$lookUpDropDownList": form.value("lcStatus$lookUpDropDownList", "1"),
            "txtScriptedBy": form.value("txtScriptedBy"),
            "hdnScriptedBy": form.value("hdnScriptedBy"),
            "txtEditedBy": form.value("txtEditedBy"),
            "hdnEditedBy": form.value("hdnEditedBy"),
            "cmbScriptFor": form.value("cmbScriptFor", "1"),
            "txtCaseReqNo": "0",
            "txtSqlScript": "",
            "hdnIsScriptFromFile": "1",
            "bpBuild$buildTextBox": "",
            "bpBuild$hdnBuildId": "0",
            "bpBuild$hdnBuildVersion": "0",
            "txtReleaseName": "",
            "hdnReleaseId": "0",
            "txtComment": "",
            "btnSave": "Save Script",
        }

        with open(file_path, "rb") as fh:
            files = {"fuScript": (filename, fh, "application/octet-stream")}
            r = self._post(url, data=data, files=files,
                           timeout=self.cfg.upload_timeout, allow_redirects=False)

        if r.status_code == 200 and "window.close()" in r.text:
            self.log(f"✓ Uploaded '{filename}'.")
            return True
        self.log(f"✗ Upload of '{filename}' returned an unexpected response "
                 f"(status {r.status_code}).", logging.ERROR)
        return False

    def upload_file_with_retry(self, file_path: str) -> UploadOutcome:
        """Upload one file, retrying the whole upload on transient failure."""
        filename = os.path.basename(file_path)
        attempts = self.cfg.max_retries + 1
        last_err: Optional[str] = None
        for i in range(1, attempts + 1):
            self._check_cancel()
            try:
                if self.upload_file(file_path):
                    return UploadOutcome(filename, True, attempts=i)
                last_err = "XPM did not confirm the upload."
            except XPMError:
                raise  # cancellation / unreachable — abort the whole run
            except Exception as exc:  # noqa: BLE001
                last_err = str(exc)
                self.log(f"Error uploading '{filename}': {exc}", logging.ERROR)
            if i < attempts:
                backoff = min(8, 2 ** (i - 1))
                self.log(f"Retrying '{filename}' ({i}/{attempts - 1}) in {backoff}s…",
                         logging.WARNING)
                time.sleep(backoff)
        return UploadOutcome(filename, False, error=last_err, attempts=attempts)

    def _parse_batch_page(self, html: str, batch_from: int, batch_to: int) -> list[BatchScript]:
        """Parse the ``NoteBookTable`` rows of one list page (fixed 8-column layout)."""
        from urllib.parse import parse_qs, urlparse
        base = self.cfg.base
        out: list[BatchScript] = []
        for row in parse_batch_rows(html, "NoteBookTable"):
            cells = row["cells"]
            if len(cells) < 7:
                continue
            try:
                batch_no = int(cells[0])
            except (ValueError, IndexError):
                continue
            if not (batch_from <= batch_no <= batch_to):
                continue
            # NoteBookTable columns: [0] Batch # [1] Process [2] File [3] Scripted By [4] Scripted On …
            process = cells[1] if len(cells) > 1 else ""
            script_name = cells[2] if len(cells) > 2 else ""
            scripted_by = cells[3] if len(cells) > 3 else ""
            scripted_on = _normalise_date(cells[4] if len(cells) > 4 else "")
            url = ""
            for href in row["links"]:
                if "sid=" in href.lower():
                    qs = parse_qs(urlparse(href).query)
                    sid = qs.get("sId", qs.get("sid", [None]))[0]
                    if sid:
                        url = f"{base}/buildmanagement/getmigrationscript.aspx?sId={sid}"
                        break
            out.append(BatchScript(batch_no, script_name, scripted_by, scripted_on, url,
                                   process=process))
        return out

    @staticmethod
    def _pager_next_target(html: str, next_page: int, visited: set) -> Optional[tuple]:
        """Find the ASP.NET GridView pager postback for the next page.
        Returns (event_target, event_argument) or None when there is no next page.
        Handles numbered pages, a 'Next' link, and '…' window jumps."""
        import re
        targets: dict[str, str] = {}
        for m in re.finditer(r"__doPostBack\('([^']+)','Page\$([^']+)'\)", html):
            targets.setdefault(m.group(2), m.group(1))  # arg -> event target
        # 1) exact next page number
        key = str(next_page)
        if key in targets and f"Page${key}" not in visited:
            return targets[key], f"Page${key}"
        # 2) explicit "Next" pager link
        if "Next" in targets and "Page$Next" not in visited:
            return targets["Next"], "Page$Next"
        # 3) smallest numbered page >= next_page (window jump), not yet visited
        nums = sorted(int(k) for k in targets if k.isdigit())
        for n in nums:
            if n >= next_page and f"Page${n}" not in visited:
                return targets[str(n)], f"Page${n}"
        return None

    def scrape_batch_links(self, batch_from: int, batch_to: int) -> list[BatchScript]:
        """Scrape ``migrationscriptlist.aspx`` for rows whose Batch No is within
        [batch_from, batch_to], following the ASP.NET GridView pager across ALL
        pages (XPM shows ~25 rows per page). Falls back to a single page when no
        pager is present, so behaviour is unchanged for un-paged lists."""
        self.log("Loading the migration-script list…")
        r = self._get(self.cfg.url("script_list"))
        r.raise_for_status()

        seen: set[int] = set()
        out: list[BatchScript] = []
        visited_args: set[str] = {"Page$1"}
        html = r.text
        page = 1

        for _ in range(500):  # hard safety cap on pages
            self._check_cancel()
            for bs in self._parse_batch_page(html, batch_from, batch_to):
                if bs.batch_no not in seen:
                    seen.add(bs.batch_no)
                    out.append(bs)
            nxt = self._pager_next_target(html, page + 1, visited_args)
            if not nxt:
                break
            target, arg = nxt
            visited_args.add(arg)
            form = FormFields.parse(html)
            data = form.viewstate_payload()
            data["__EVENTTARGET"] = target
            data["__EVENTARGUMENT"] = arg
            data.update(form.extra_post_fields(exclude=set(data)))
            self.log(f"Fetching list page {page + 1}…", logging.DEBUG)
            r = self._post(self.cfg.url("script_list"), data=data, timeout=self.cfg.timeout)
            r.raise_for_status()
            html = r.text
            page += 1

        out.sort(key=lambda b: b.batch_no)
        self.log(f"Found {len(out)} script(s) across {page} list page(s).")
        return out

    def list_all_batches(self) -> list[BatchScript]:
        """Every script currently in the project (newest first) — for the explorer."""
        rows = self.scrape_batch_links(0, 9_999_999)
        rows.sort(key=lambda b: b.batch_no, reverse=True)
        return rows

    def list_projects(self) -> list[dict]:
        """Log in and scrape the global project dropdown from the home page.
        Returns ``[{'id': <project id>, 'name': <project name>}, …]`` so the UI
        can offer a live picker instead of hand-typed project id/name."""
        self.login()
        url = f"{self.cfg.base}/sdghome.aspx"
        self.log("Loading the project list…")
        r = self._get(url)
        r.raise_for_status()
        opts = parse_select_options(r.text, select_id="ctl00_header_cmbProject")
        return [{"id": val, "name": text} for val, text in opts if val and text]

    def list_processes(self) -> list[dict]:
        """Log in, switch to the configured project, and scrape the process
        dropdown on the new-migration-script page.
        Returns ``[{'value': <cmbProcess value>, 'name': <process name>}, …]``."""
        self.login()
        self.select_project()
        self.log("Loading the process list…")
        r = self._get(self.cfg.url("new_script"))
        r.raise_for_status()
        opts = parse_select_options(r.text, select_name="cmbProcess")
        return [{"value": val, "name": text} for val, text in opts if text]

    def download_consolidated(self, num_uploaded: int) -> tuple[bytes, int, int]:
        """Download the consolidated script for exactly the ``num_uploaded`` most
        recent batches. Returns (content, batch_from, batch_to).

        This is the upgraded desktop flow: rather than a blind GET, we find the
        batch numbers of the scripts we just uploaded (the tail of the list) and
        download precisely that range via the results-page form flow."""
        self.log("Scraping the batch list to locate the newly uploaded scripts…")
        batches = self.scrape_batch_links(0, 9_999_999)
        if not batches:
            raise XPMError("Could not find any scripts in the project after upload.")
        recent = batches[-num_uploaded:] if num_uploaded > 0 else batches
        batch_from = recent[0].batch_no
        batch_to = recent[-1].batch_no
        self.log(f"Downloading consolidated script for batches #{batch_from}–#{batch_to}…")
        content = self.download_batch_range(batch_from, batch_to)
        return content, batch_from, batch_to

    def download_batch_range(self, batch_from: int, batch_to: int) -> bytes:
        """Replay the browser flow to download & merge a batch range into one
        file. Faithful port of the desktop ``_download_via_form``."""
        base = self.cfg.base
        url = self.cfg.url("download")

        self.log("Loading the migration-script page…")
        r = self._get(url)
        r.raise_for_status()
        form = FormFields.parse(r.text)
        vs = form.viewstate_payload()

        process_value = ""
        proc_sel = "ctl00$ViewListSection$lsbProcess"
        if form.has_select(proc_sel):
            process_value = form.value(proc_sel, "")

        range_url = f"{url}?sbn={batch_from}&ebn={batch_to}"
        self.log(f"Submitting batch range #{batch_from} → #{batch_to}…")
        r = self._post(range_url, timeout=60, allow_redirects=False, data={
            **vs,
            "ctl00$ViewListSection$bnStart$bnTextBox": str(batch_from),
            "ctl00$ViewListSection$bnEnd$bnTextBox": str(batch_to),
            "ctl00$ViewListSection$btnGo.x": "1",
            "ctl00$ViewListSection$btnGo.y": "1",
            "ctl00$ViewListSection$lsbProcess": process_value,
            "ctl00$ViewListSection$hdnProcess": process_value,
        })
        if r.status_code in (301, 302, 303, 307, 308) and "Location" in r.headers:
            r = self._get(self._abs_url(base, r.headers["Location"]), allow_redirects=True)
            r.raise_for_status()
        elif r.status_code != 200:
            raise XPMError(f"Batch-range submit returned unexpected status {r.status_code}.")

        results = FormFields.parse(r.text)
        vs2 = results.viewstate_payload()

        data = {
            **vs2,
            "ctl00$ViewListSection$bnStart$bnTextBox": str(batch_from),
            "ctl00$ViewListSection$bnEnd$bnTextBox": str(batch_to),
            "ctl00$ViewListSection$lsbProcess": process_value,
            "ctl00$ViewListSection$hdnProcess": process_value,
            "ctl00$ViewListSection$btnDownload": "Download as txt file",
        }
        # Forward the results-page hidden fields + checked checkboxes exactly as a
        # browser would (some XPM builds require the per-row selection state).
        data.update(results.extra_post_fields(exclude=set(data)))

        self.log("Requesting the merged download…")
        r = self._post(range_url, timeout=self.cfg.download_timeout,
                       allow_redirects=False, data=data)
        if r.status_code in (301, 302, 303, 307, 308) and "Location" in r.headers:
            r = self._get(self._abs_url(base, r.headers["Location"]),
                          timeout=self.cfg.download_timeout, allow_redirects=True)
        elif r.status_code != 200:
            raise XPMError(f"Download request returned unexpected status {r.status_code}.")
        r.raise_for_status()

        content_type = r.headers.get("Content-Type", "")
        if "text/html" in content_type or not r.content:
            raise XPMError(
                f"XPM returned HTML instead of a file for batch range "
                f"{batch_from}–{batch_to}. The range may contain no scripts."
            )
        self.log(f"Received {len(r.content):,} bytes for batches #{batch_from}–#{batch_to}.")
        return r.content

    def close(self) -> None:
        try:
            self.session.close()
        except Exception:  # noqa: BLE001
            pass


def _normalise_date(raw: str) -> str:
    """Normalise whatever date string XPM shows into 'YYYY-MM-DD HH:MM:SS'."""
    import re
    from datetime import datetime
    raw = (raw or "").strip()
    if not raw:
        return ""
    fmts = [
        "%Y-%m-%d %H:%M:%S", "%Y/%m/%d %H:%M:%S",
        "%d-%m-%Y %H:%M:%S", "%d/%m/%Y %H:%M:%S",
        "%m/%d/%Y %H:%M:%S", "%m-%d-%Y %H:%M:%S",
        "%Y-%m-%d", "%d/%m/%Y", "%m/%d/%Y",
    ]
    for fmt in fmts:
        try:
            return datetime.strptime(raw, fmt).strftime("%Y-%m-%d %H:%M:%S")
        except ValueError:
            continue
    return raw if re.search(r"\d", raw) else ""
