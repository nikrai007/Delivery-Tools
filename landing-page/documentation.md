# Landing Page (blueprint: `landing`)

The platform launchpad at `/` plus the `/about` page. Designed in Google Stitch,
then implemented in the platform's own design system (indigo brand palette,
Inter + JetBrains Mono, Material Symbols, aurora-blob backdrop, glass cards) with
full light/dark support.

## Layout

| Path | Role |
|---|---|
| `source-code/landing_routes.py` | Blueprint `landing`. Serves `/` (hub) and `/about`. Holds the `LANDING_TOOLS` registry. |
| `templates/landing.html` | The hub: sticky glass navbar, hero, **tools grid**, features strip, CTA band, footer. |
| `templates/about.html` | Platform/tool about page. |
| `assets/` | Brand assets owned by the landing page (`logo.svg`, `favicon.svg`). |
| `styles/` | Landing-specific styling notes (see below). |
| `components/` | Section breakdown notes (see below). |

## The tools grid is data-driven

The grid renders from `LANDING_TOOLS` in `landing_routes.py`. To add a tool card,
append a dict:

```python
{
    "name": "Schema Diff",
    "icon": "difference",                 # Material Symbols name
    "desc": "Visual diff between two Oracle schemas.",
    "tags": ["DEV", "UAT", "PROD"],
    "status": "live",                     # or "soon"
    "endpoint": "schemadiff.home",        # required when status == "live"
}
```

Live cards link to their tool's endpoint; `soon` cards render dimmed with a
**SOON** pill. A dashed "More tools coming" placeholder always trails the grid.

## Styling

- The **shared design system** (`/static/style.css` — aurora backdrop, glass
  cards, button sheen) is reused so the hub matches every tool page.
- A small block of **landing-only** helpers (scroll-reveal fade-ups, tool-card
  hover lift) is inlined in `landing.html`. Keep page-specific CSS there; put
  anything reusable into the shared `style.css`.

## Components (section breakdown)

`landing.html` is one file composed of these sections, top to bottom:
**navbar → hero → tools grid (`#toolbox`) → features (`#features`) → CTA band →
footer.** If a section grows complex enough to reuse, extract it into a Jinja
partial under `components/` and `{% include %}` it.
