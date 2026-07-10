"""
xpm_core — service layer for the XPM Automator tool.

Clean separation of concerns, each module single-purpose and independently
testable:

    html_forms  — dependency-free ASP.NET WebForms field/VIEWSTATE extractor
    config      — XPMConfig value object + validation + settings resolution
    batch       — Batch Number generation / validation / filename slugging
    client      — XPMClient: the HTTP conversation with the XPM CRM (login,
                  project select, upload, download) — no Flask, no DB
    pipeline    — orchestration: background run worker, retry, progress registry

Nothing in this package imports Flask, so the whole surface is unit-testable in
isolation from the web layer.
"""
