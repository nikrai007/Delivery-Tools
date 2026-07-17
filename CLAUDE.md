# Delivery Toolbox

Self-hosted, single-login Flask platform hosting a growing set of independent internal tools (AutoBackupRevert, XPM Automator, Release Tracker, team management, …), one shared design system.

## Design Context

Design intent lives in two root files — read them before changing any UI:

- **[PRODUCT.md](PRODUCT.md)** — strategy. Register **product**, platform **web**. Users: internal teams across the wider org **plus external clients**, self-serving operational delivery tasks. Positioning: **"one login, many safe tools."** Personality: **modern & premium**. Explicitly avoids generic Bootstrap admin, cluttered legacy enterprise, playful consumer/startup, and sterile personality-free looks. Accessibility target: **WCAG 2.1 AA**, broadly adoptable.
- **[DESIGN.md](DESIGN.md)** — visual system. North star **"The Glass Instrument"**: glass/aurora carries the *frame* (auth, topbar — premium first impression); crisp, utilitarian surfaces carry the *task*. **Iris** indigo (`#6366f1` / `#4f46e5`) is the single action-and-selection voice over zinc neutrals; **Inter** for all UI text, **JetBrains Mono** for code/SQL; soft ambient depth with a colored glow reserved for primary buttons. Machine-readable tokens are in DESIGN.md frontmatter; component snippets and rules in `.impeccable/design.json`.

Run `/impeccable` for design work on this project; it reads both files first.
