# components — landing page

`../templates/landing.html` is a single template composed of these sections,
top to bottom:

1. **Navbar** — sticky glass bar; brand mark + nav links + theme toggle + Sign in.
2. **Hero** — pill badge, gradient headline ("release-safety"), subhead, two CTAs.
3. **Tools grid** (`#toolbox`) — cards rendered from the `LANDING_TOOLS` registry
   (live cards clickable, `soon` cards dimmed) + a dashed "More tools" placeholder.
4. **Features** (`#features`) — four why-this-platform blurbs.
5. **CTA band** — closing call-to-action into AutoBackupRevert.
6. **Footer** — link columns + status + attribution.

If a section grows complex or is reused, extract it into a Jinja partial here
(e.g. `_tool_card.html`) and `{% include %}` it from `landing.html`.
