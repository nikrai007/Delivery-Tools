"""
Dependency-free HTML form parser for ASP.NET WebForms pages.

The standalone XPM tool leaned on BeautifulSoup purely to read hidden fields
(``__VIEWSTATE`` / ``__EVENTVALIDATION`` / …), current input values, and to
match a ``<select>`` option by its visible text. That is a small, well-bounded
job the Python standard library's :class:`html.parser.HTMLParser` handles
perfectly — so this tool ships with **no new third-party dependency** (only
``requests``, already vendored by the platform).

Usage::

    form = FormFields.parse(html)
    vs   = form.value("__VIEWSTATE")
    proc = form.option_value_by_text("cmbProcess", "SBC Gold 8 Upgrade")
    hidden = form.viewstate_payload()          # dict ready to merge into a POST

Robustness notes:
  * Unknown / malformed markup never raises — the parser is lenient by design
    (``convert_charrefs=True``, best-effort attribute reads).
  * ``value()`` resolves inputs, the selected option of a ``<select>``, and
    ``<textarea>`` text, mirroring the precedence the original ``_form_val``
    used, so behaviour is byte-compatible with the proven desktop flow.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import Optional


# Canonical ASP.NET postback hidden fields harvested for every WebForms POST.
VIEWSTATE_FIELDS = (
    "__VIEWSTATE",
    "__VIEWSTATEGENERATOR",
    "__EVENTVALIDATION",
    "__EVENTTARGET",
    "__EVENTARGUMENT",
    "__LASTFOCUS",
)


@dataclass
class _Option:
    value: str
    text: str
    selected: bool


@dataclass
class _Select:
    options: list = field(default_factory=list)  # list[_Option]

    @property
    def selected_value(self) -> Optional[str]:
        for opt in self.options:
            if opt.selected:
                return opt.value
        # ASP.NET renders the first option as the implicit selection when none
        # is explicitly marked — mirror a browser's behaviour.
        return self.options[0].value if self.options else None


class _FormParser(HTMLParser):
    """Collects inputs, selects (with their options) and textareas by name."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.inputs: dict[str, str] = {}
        # Richer per-input metadata (type/checked) so callers can replicate the
        # exact WebForms POST body — hidden fields + checked checkboxes.
        self.input_meta: dict[str, dict] = {}
        self.selects: dict[str, _Select] = {}
        self.textareas: dict[str, str] = {}
        self._cur_select_name: Optional[str] = None
        self._cur_option: Optional[_Option] = None
        self._cur_textarea_name: Optional[str] = None

    # -- helpers -------------------------------------------------------------
    @staticmethod
    def _attr(attrs: list[tuple[str, Optional[str]]], key: str) -> Optional[str]:
        for k, v in attrs:
            if k.lower() == key:
                return v if v is not None else ""
        return None

    @staticmethod
    def _has(attrs: list[tuple[str, Optional[str]]], key: str) -> bool:
        return any(k.lower() == key for k, _ in attrs)

    # -- tag handlers --------------------------------------------------------
    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        if tag == "input":
            name = self._attr(attrs, "name")
            if name:
                # Later fields on the page win — matches how a real form serialises.
                self.inputs[name] = self._attr(attrs, "value") or ""
                self.input_meta[name] = {
                    "type": (self._attr(attrs, "type") or "text").lower(),
                    "checked": self._has(attrs, "checked"),
                    "value": self._attr(attrs, "value") or "",
                }
        elif tag == "select":
            name = self._attr(attrs, "name")
            if name:
                self._cur_select_name = name
                self.selects.setdefault(name, _Select())
        elif tag == "option" and self._cur_select_name is not None:
            self._cur_option = _Option(
                value=self._attr(attrs, "value") or "",
                text="",
                selected=self._has(attrs, "selected"),
            )
        elif tag == "textarea":
            name = self._attr(attrs, "name")
            if name:
                self._cur_textarea_name = name
                self.textareas.setdefault(name, "")

    def handle_data(self, data):
        if self._cur_option is not None:
            self._cur_option.text += data
        elif self._cur_textarea_name is not None:
            self.textareas[self._cur_textarea_name] += data

    def handle_endtag(self, tag):
        tag = tag.lower()
        if tag == "option" and self._cur_option is not None and self._cur_select_name:
            opt = self._cur_option
            opt.value = opt.value if opt.value else opt.text.strip()
            self.selects[self._cur_select_name].options.append(opt)
            self._cur_option = None
        elif tag == "select":
            self._cur_select_name = None
        elif tag == "textarea":
            self._cur_textarea_name = None


@dataclass
class FormFields:
    """Parsed, queryable view of a WebForms page's fields."""

    inputs: dict
    selects: dict
    textareas: dict
    input_meta: dict

    @classmethod
    def parse(cls, html: str) -> "FormFields":
        p = _FormParser()
        try:
            p.feed(html or "")
        except Exception:  # noqa: BLE001 — malformed markup must never crash a run
            pass
        return cls(inputs=p.inputs, selects=p.selects, textareas=p.textareas,
                   input_meta=p.input_meta)

    # -- queries -------------------------------------------------------------
    def value(self, name: str, default: str = "") -> str:
        """Resolve a field's current value: input → selected option → textarea."""
        if name in self.inputs:
            return self.inputs[name]
        if name in self.selects:
            sv = self.selects[name].selected_value
            return sv if sv is not None else default
        if name in self.textareas:
            return self.textareas[name].strip()
        return default

    def option_value_by_text(self, select_name: str, text: str) -> Optional[str]:
        """Return the ``value`` of the option whose visible text matches ``text``
        (exact, whitespace-trimmed). ``None`` if the select/option is absent."""
        sel = self.selects.get(select_name)
        if not sel:
            return None
        target = (text or "").strip()
        for opt in sel.options:
            if opt.text.strip() == target:
                return opt.value
        return None

    def has_select(self, select_name: str) -> bool:
        return select_name in self.selects

    def viewstate_payload(self) -> dict:
        """The standard postback hidden fields as a ready-to-merge POST dict."""
        return {name: self.value(name, "") for name in VIEWSTATE_FIELDS}

    def extra_post_fields(self, exclude: set | None = None) -> dict:
        """Hidden inputs (always) + checked checkboxes (value or ``on``) that a
        browser would submit — used to faithfully replicate the XPM download
        POST. Names already handled by the caller are skipped via ``exclude``."""
        exclude = exclude or set()
        out: dict[str, str] = {}
        for name, meta in self.input_meta.items():
            if name in exclude or name in out:
                continue
            t = meta.get("type", "text")
            if t == "hidden":
                out[name] = meta.get("value", "")
            elif t == "checkbox" and meta.get("checked"):
                out[name] = meta.get("value") or "on"
        return out


# --------------------------------------------------------------------------
# Batch-list table scraper (migrationscriptlist.aspx)
# --------------------------------------------------------------------------
class _BatchRowParser(HTMLParser):
    """Extracts ``<tr class="…NoteBookTable…">`` rows: each cell's text plus any
    ``sId=`` link in the row. Mirrors the standalone's fixed 8-column layout:
    [0] Batch No · [1] Process · [2] File · [3] Scripted By · [4] Scripted On …"""

    def __init__(self, row_class: str) -> None:
        super().__init__(convert_charrefs=True)
        self._row_class = row_class.lower()
        self.rows: list[dict] = []
        self._in_row = False
        self._in_cell = False
        self._cells: list[str] = []
        self._links: list[str] = []
        self._cur_text = ""

    @staticmethod
    def _attr(attrs, key):
        for k, v in attrs:
            if k.lower() == key:
                return v or ""
        return None

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        if tag == "tr":
            cls = (self._attr(attrs, "class") or "").lower()
            if self._row_class in cls:
                self._in_row, self._cells, self._links = True, [], []
        elif tag == "td" and self._in_row:
            self._in_cell, self._cur_text = True, ""
        elif tag == "a" and self._in_row:
            href = self._attr(attrs, "href") or ""
            if href:
                self._links.append(href)

    def handle_data(self, data):
        if self._in_cell:
            self._cur_text += data

    def handle_endtag(self, tag):
        tag = tag.lower()
        if tag == "td" and self._in_row and self._in_cell:
            self._cells.append(self._cur_text.replace("\xa0", " ").strip())
            self._in_cell = False
        elif tag == "tr" and self._in_row:
            self.rows.append({"cells": self._cells, "links": self._links})
            self._in_row = False


def parse_batch_rows(html: str, row_class: str = "NoteBookTable") -> list[dict]:
    """Return ``[{cells: [...], links: [...]}, ...]`` for every matching row."""
    p = _BatchRowParser(row_class)
    try:
        p.feed(html or "")
    except Exception:  # noqa: BLE001
        pass
    return p.rows


# --------------------------------------------------------------------------
# Generic <select> option scraper (match by id OR name)
# --------------------------------------------------------------------------
class _SelectParser(HTMLParser):
    """Collects the ``(value, text)`` options of the first ``<select>`` matching
    a given id or name — used to read the XPM project / process dropdowns."""

    def __init__(self, select_id: Optional[str], select_name: Optional[str]) -> None:
        super().__init__(convert_charrefs=True)
        self._id = select_id
        self._name = select_name
        self.options: list[tuple[str, str]] = []
        self._done = False
        self._in_target = False
        self._cur_val: Optional[str] = None
        self._cur_text = ""

    @staticmethod
    def _attr(attrs, key):
        for k, v in attrs:
            if k.lower() == key:
                return v or ""
        return None

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        if tag == "select" and not self._done:
            sid = self._attr(attrs, "id")
            sname = self._attr(attrs, "name")
            self._in_target = bool((self._id and sid == self._id) or
                                   (self._name and sname == self._name))
        elif tag == "option" and self._in_target:
            self._cur_val = self._attr(attrs, "value")
            self._cur_text = ""

    def handle_data(self, data):
        if self._in_target and self._cur_val is not None:
            self._cur_text += data

    def handle_endtag(self, tag):
        tag = tag.lower()
        if tag == "option" and self._in_target and self._cur_val is not None:
            text = self._cur_text.strip()
            self.options.append((self._cur_val or text, text))
            self._cur_val = None
        elif tag == "select" and self._in_target:
            self._in_target = False
            self._done = True  # only the first matching select


def parse_select_options(html: str, *, select_id: str | None = None,
                         select_name: str | None = None) -> list[tuple[str, str]]:
    """Return ``[(value, text), ...]`` for the first ``<select>`` whose id or
    name matches. Empty list if not found."""
    p = _SelectParser(select_id, select_name)
    try:
        p.feed(html or "")
    except Exception:  # noqa: BLE001
        pass
    return p.options
