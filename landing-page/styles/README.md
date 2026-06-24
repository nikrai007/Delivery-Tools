# styles — landing page

The landing page reuses the **shared design system** served from the platform
static dir: `/static/style.css` (aurora backdrop, glass cards, button sheen,
logo glow). That file is the single source of truth for the look that every tool
shares.

Landing-**only** helpers (scroll-reveal fade-ups, tool-card hover lift) are a
small `<style>` block inlined at the top of `../templates/landing.html`. Keep
page-specific tweaks there; promote anything reusable into the shared
`static/style.css`.
