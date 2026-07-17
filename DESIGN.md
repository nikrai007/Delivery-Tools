---
name: Delivery Toolbox
description: One login, many safe tools — a premium, self-hosted platform for internal delivery operations.
colors:
  iris: "#6366f1"
  iris-strong: "#4f46e5"
  iris-soft: "#818cf8"
  iris-wash: "#eef2ff"
  iris-deep: "#312e81"
  ink: "#18181b"
  ink-soft: "#71717a"
  canvas: "#fafafa"
  surface: "#ffffff"
  canvas-dark: "#09090b"
  surface-dark: "#18181b"
  border: "#e4e4e7"
  border-dark: "#27272a"
  danger: "#e11d48"
  warning: "#f59e0b"
  success: "#10b981"
  info: "#0ea5e9"
typography:
  display:
    fontFamily: "Inter, system-ui, -apple-system, Segoe UI, Roboto, sans-serif"
    fontSize: "1.5rem"
    fontWeight: 700
    lineHeight: 1.2
    letterSpacing: "-0.02em"
  headline:
    fontFamily: "Inter, system-ui, sans-serif"
    fontSize: "1.125rem"
    fontWeight: 600
    lineHeight: 1.3
    letterSpacing: "-0.01em"
  title:
    fontFamily: "Inter, system-ui, sans-serif"
    fontSize: "1rem"
    fontWeight: 600
    lineHeight: 1.4
    letterSpacing: "normal"
  body:
    fontFamily: "Inter, system-ui, sans-serif"
    fontSize: "0.875rem"
    fontWeight: 400
    lineHeight: 1.6
    letterSpacing: "normal"
  label:
    fontFamily: "Inter, system-ui, sans-serif"
    fontSize: "0.625rem"
    fontWeight: 600
    lineHeight: 1.2
    letterSpacing: "0.05em"
  mono:
    fontFamily: "JetBrains Mono, ui-monospace, SFMono-Regular, Menlo, Consolas, monospace"
    fontSize: "0.8125rem"
    fontWeight: 400
    lineHeight: 1.5
    letterSpacing: "normal"
rounded:
  md: "0.375rem"
  lg: "0.625rem"
  xl: "0.875rem"
  2xl: "1rem"
  full: "9999px"
spacing:
  xs: "4px"
  sm: "8px"
  md: "16px"
  lg: "24px"
  xl: "32px"
components:
  button-primary:
    backgroundColor: "{colors.iris-strong}"
    textColor: "{colors.surface}"
    typography: "{typography.body}"
    rounded: "{rounded.xl}"
    padding: "12px 16px"
  button-primary-hover:
    backgroundColor: "{colors.iris-deep}"
    textColor: "{colors.surface}"
    rounded: "{rounded.xl}"
    padding: "12px 16px"
  nav-item-active:
    backgroundColor: "{colors.iris-wash}"
    textColor: "{colors.iris-strong}"
    typography: "{typography.body}"
    rounded: "{rounded.lg}"
    padding: "8px 12px"
  input:
    backgroundColor: "{colors.surface}"
    textColor: "{colors.ink}"
    typography: "{typography.body}"
    rounded: "{rounded.xl}"
    padding: "10px 12px"
  card:
    backgroundColor: "{colors.surface}"
    textColor: "{colors.ink}"
    rounded: "{rounded.xl}"
    padding: "24px"
  badge-pill:
    backgroundColor: "{colors.warning}"
    textColor: "{colors.surface}"
    typography: "{typography.label}"
    rounded: "{rounded.full}"
    padding: "2px 6px"
---

# Design System: Delivery Toolbox

## 1. Overview

**Creative North Star: "The Glass Instrument"**

Delivery Toolbox is a precise instrument you trust with production data. The system reads as modern and premium — polished, current, quietly impressive — because it has to earn the confidence of internal teams running risky operations *and* the trust of external clients evaluating the platform for the first time. The premium impression is carried by the frame: the aurora-and-glass authentication surfaces, the backdrop-blurred sticky topbar, the breathing glow behind the brand mark. That frame is the handshake.

Inside the frame, the working surfaces are crisp and utilitarian. Once a user is in a task — generating an FK-safe Oracle rollback, batching an XPM upload, reading a release tracker — the interface gets out of the way. Dense, legible, single-family type; flat working cards; a resizable, collapsible sidebar that yields the screen to the content. The glass is for arrival and identity; the instrument is for work. Depth is atmospheric rather than heavy: soft ambient shadows, a colored Iris glow reserved for primary actions, and glass only where it means "this is the platform speaking," never as decoration on a data table.

Every tool is built from this one system, so a new tool is recognizable as part of the whole on first glance — the launchpad-plus-tools model made visible. The system explicitly rejects the **generic Bootstrap admin** look (boxy stock cards, template anonymity), the **cluttered legacy-enterprise** feel (grey, cramped, intimidating Oracle-Forms density), **playful consumer/startup** flourishes (gradients everywhere, emoji, bounce), and **sterile personality-free** plainness (unfinished, unloved, forgettable). It is finished but never flippant; serious but never drab.

**Key Characteristics:**
- Iris-indigo as the single voice of action and selection, against zinc neutrals.
- One type family (Inter) doing headings, labels, body, and data; JetBrains Mono for code and SQL.
- Glass and aurora at the frame; flat, crisp surfaces in the task.
- Light and dark are co-equal, class-toggled, and remembered before first paint.
- Soft ambient depth with a reserved colored glow, never structural heaviness.

## 2. Colors

A disciplined Iris-indigo accent over a cool zinc neutral field, with a tight semantic set for state. Color is a signal, never decoration.

### Primary
- **Iris** (`#6366f1`, brand-500): The single voice of the system. Reserved for primary actions, the current selection/active nav item, focus rings, and state indicators. It is the only saturated color allowed to carry meaning on a working surface.
- **Iris Strong** (`#4f46e5`, brand-600): The committed action tone — solid primary buttons, avatar fallbacks, active links. The color of "do it."
- **Iris Soft** (`#818cf8`, brand-400): The dark-mode focus and hover partner; brightens Iris where a dark field would swallow brand-500.
- **Iris Wash** (`#eef2ff`, brand-50): The quiet tint behind an active nav item or an unread notification in light mode. Presence without shouting.
- **Iris Deep** (`#312e81`, brand-900): Pressed/hover depth and dark-mode active washes.

### Neutral
- **Ink** (`#18181b`, zinc-900): Primary text on light; the sidebar/surface in dark mode.
- **Ink Soft** (`#71717a`, zinc-500): Secondary text, metadata, muted labels. Never lighter than this for body-adjacent text — the readability floor.
- **Canvas** (`#fafafa`, zinc-50): The light app background, one step below surface.
- **Surface** (`#ffffff`): Cards, sidebar, panels, and menus in light mode.
- **Canvas Dark** (`#09090b`, zinc-950) / **Surface Dark** (`#18181b`, zinc-900): The dark-mode background and raised-surface pair — the second neutral layer that separates content from chrome.
- **Border** (`#e4e4e7`, zinc-200) / **Border Dark** (`#27272a`, zinc-800): Hairline dividers, card edges, sidebar rule.

### Tertiary — Semantic state
- **Danger** (`#e11d48`, rose-600): Destructive actions, sign-out, error badges. The only red on the surface.
- **Warning** (`#f59e0b`, amber-500): Pending states, session-expiry, count badges awaiting attention.
- **Success** (`#10b981`, emerald-500): Completed, healthy, confirmed.
- **Info** (`#0ea5e9`, sky-500): Neutral informational accents.

*Aurora note:* the auth backdrop drifts Iris, emerald, sky, and amber blobs at 10–28% opacity behind heavy blur. Those are atmosphere on the unauthenticated frame only — never lift them onto a working surface.

### Named Rules
**The One Voice Rule.** Iris is the only saturated hue permitted to carry meaning on a working (authenticated) surface. If two things are both "brand-colored" on one screen, one of them is wrong. State colors (danger/warning/success/info) speak only for their state.

**The Cool-Neutral Rule.** Neutrals are zinc, never warm. No cream, sand, or beige backgrounds — the warmth in this brand is carried by Iris and finish, not by a tinted canvas.

## 3. Typography

**Display / Headline / Body / Label Font:** Inter (with system-ui, -apple-system, Segoe UI, Roboto fallback)
**Mono Font:** JetBrains Mono (with ui-monospace, SFMono-Regular, Menlo, Consolas)
**Icon Font:** Material Symbols Outlined (weight 400, optical size 20, fill 0)

**Character:** One well-tuned humanist sans carries the entire product — headings, buttons, labels, dense data — so nothing ever looks like a mismatched control. Contrast comes from weight and size on a tight scale, not from a second family. JetBrains Mono appears only where characters must align: SQL, batch numbers, logs, generated scripts.

### Hierarchy
- **Display** (Inter 700, 1.5rem, line-height 1.2, tracking -0.02em): Modal titles, landing/marketing headings, the largest text in the UI. Deliberately modest — this is a tool, not a billboard.
- **Headline** (Inter 600, 1.125rem / text-lg, line-height 1.3): Page titles in the topbar; primary section headings.
- **Title** (Inter 600, 1rem, line-height 1.4): Card headers, panel titles, grouped-form headings.
- **Body** (Inter 400, 0.875rem / text-sm, line-height 1.6): The workhorse. Task copy, form values, table cells. Prose caps at 65–75ch; data tables may run denser.
- **Label** (Inter 600, 0.625rem / 10px, tracking 0.05em, uppercase): Nav-group headers, role chips, metadata eyebrows *inside components*. Micro-labels also live at 11px.
- **Mono** (JetBrains Mono 400, 0.8125rem): SQL, batch numbers, logs, generated scripts. Tabular-nums for aligned figures.

### Named Rules
**The One Family Rule.** Inter does everything except code. Never introduce a display or "brand" font for headings — exaggerated type contrast reads as noise on a product surface, and a second family breaks the single-system promise.

**The Modest Display Rule.** The largest UI text tops out around 1.5rem. No fluid `clamp()` hero type in the app; users view at consistent DPI and a shrinking h1 in a sidebar looks worse, not better.

## 4. Elevation

Soft ambient depth with a reserved colored glow. Surfaces are separated primarily by a hairline zinc border and a second tonal layer (chrome vs. content); shadow is the gentle atmosphere on top, not the primary separator. Glass (backdrop-blur) is a deliberate material reserved for the sticky topbar and the auth frame — where it signals "the platform," not "a card."

### Shadow Vocabulary
- **Hairline** (`box-shadow: 0 1px 2px rgba(24,24,27,0.05)` / shadow-sm): The sticky topbar's whisper of separation over scrolling content.
- **Ambient Card** (`box-shadow: 0 8px 32px rgba(24,24,27,0.07)`): The glass auth card and raised panels. Diffuse, wide, low-opacity.
- **Menu** (`box-shadow: 0 10px 15px -3px rgba(24,24,27,0.1)` / shadow-lg): Dropdowns — notification and profile panels.
- **Modal** (shadow-2xl): Dialog surfaces, lifted clearly above the blurred scrim.
- **Iris Glow** (`box-shadow: 0 4px 6px rgba(99,102,241,0.25)` / shadow-md shadow-brand-500/25): The *only* colored shadow — reserved for primary buttons, so the main action reads as gently lit from within.

### Named Rules
**The Reserved Glow Rule.** Colored shadow belongs to primary actions and nothing else. A card, an input, or a table row never glows Iris; the glow's rarity is what makes the primary button obvious.

**The Glass-Is-The-Platform Rule.** `backdrop-filter` blur is permitted only on the topbar and the unauthenticated frame. It is forbidden as a decorative treatment on working cards, tables, or tool panels.

## 5. Components

### Buttons
- **Shape:** Gently curved (14px / rounded-xl for primary and modal actions; 6px / rounded-md for compact icon buttons).
- **Primary:** Iris Strong fill (a subtle top-lit `from-brand-500 to-brand-700` gradient on hero actions), white text, `12px 16px` padding, the Iris Glow shadow. A vertical sheen sweep (`.btn-sheen`) passes on hover.
- **Hover / Focus / Active:** Lift `-translate-y-0.5` on hover, press to `scale(0.98)` on active; visible focus ring in Iris. Transitions 150–200 ms.
- **Ghost / Icon:** Transparent at rest; zinc-100 (dark: zinc-800) wash on hover. Destructive icon buttons shift to Danger with a rose wash.

### Navigation (Sidebar)
- **Shell:** Fixed 16rem left rail, resizable (drag, 200–420px) and collapsible on desktop, off-canvas drawer on mobile. White (dark: zinc-900) with a zinc border.
- **Item:** `rounded-lg`, `8px 12px`, transition-colors 150 ms. Idle is Ink Soft; hover lifts text toward Ink over a zinc-100 wash.
- **Active:** Iris Strong text on an Iris Wash background, `font-semibold`. The single clearest "you are here" signal.
- **Groups:** Collapsible sections with uppercase 10px labels; state persisted per user.

### Inputs / Fields
- **Style:** White (dark: surface-dark) fill, zinc border, `rounded-xl`, `10px 12px`. Body type.
- **Focus:** `.input-glow` — border shifts to Iris and a 3px `rgba(99,102,241,0.20)` ring appears via box-shadow (no layout shift). Dark mode uses Iris Soft.
- **Error / Disabled:** Danger border + helper text for errors; reduced opacity and `not-allowed` cursor for disabled.

### Cards / Panels
- **Corner:** `rounded-xl` (14px); modals `rounded-2xl` (16px).
- **Background:** Surface (dark: surface-dark) over Canvas. Working cards are flat — border-defined, not shadowed.
- **Border:** 1px zinc hairline. **Nested cards are forbidden.**
- **Padding:** 24px interior (space-y-6 rhythm between blocks).

### Badges / Chips
- **Count badges:** `rounded-full`, Warning (amber) or Danger (rose) fill, white 10px bold text — pending requests, unread notifications.
- **Role chips:** uppercase 9–10px, zinc wash, tracked. Identity, not action.

### Glass Auth Card (Signature)
- Semi-transparent white (`rgba(255,255,255,0.72)`, dark: `rgba(24,24,27,0.68)`), `backdrop-filter: blur(16px) saturate(180%)`, Iris-tinted top border, Ambient Card shadow, and a shimmer sweep on hover. Sits over the drifting aurora backdrop with a faint noise overlay. This is the platform's handshake — premium, and confined to the unauthenticated frame.

### Session-Warning Modal (Signature)
- `rounded-2xl` dialog over a `backdrop-blur-sm` zinc-900/60 scrim, amber-accented (timer icon in an amber disc), with a gradient Iris primary action. The one place a modal is the right answer: an inactivity deadline that must interrupt.

## 6. Do's and Don'ts

### Do:
- **Do** keep Iris (`#6366f1` / `#4f46e5`) as the single action-and-selection voice; let zinc neutrals carry everything else.
- **Do** use one family (Inter) for all UI text and JetBrains Mono only for code, SQL, batch numbers, and logs.
- **Do** define every interactive state — default, hover, focus, active, disabled, loading, error — for every control; ship none of them half-done.
- **Do** reserve `backdrop-filter` glass for the topbar and auth frame, and the colored Iris glow for primary buttons only.
- **Do** honor `prefers-reduced-motion`: the CSS already disables aurora drift, breathing glow, shimmer, and button sheen — keep every new animation behind the same guard.
- **Do** teach on arrival — meaningful empty states, plain error copy, visible affordances — because outside clients and unfamiliar teams self-serve.
- **Do** keep working surfaces flat and border-defined; use the second tonal layer (chrome vs. content) to separate, not heavy shadow.

### Don't:
- **Don't** ship the **generic Bootstrap admin** look: boxy stock cards, template icons, indistinct layouts. Every screen must read as *this* product.
- **Don't** recreate **cluttered legacy-enterprise** density — grey, cramped, intimidating Oracle-Forms walls of fields. Dense is fine; oppressive is not.
- **Don't** go **playful consumer/startup**: no gradients-everywhere, no emoji, no bounce/elastic easing. This is production operations and client trust.
- **Don't** ship **sterile, personality-free** plainness either; the glass frame and Iris finish are the craft that keeps it from feeling unfinished.
- **Don't** introduce a second type family or fluid `clamp()` hero type inside the app.
- **Don't** nest cards, and never use a `border-left`/`border-right` colored stripe as an accent — use full hairline borders, a wash, or a leading icon.
- **Don't** let muted text drop below Ink Soft (`#71717a`) for anything body-adjacent, and never place gray text on an Iris background — use a white or Iris-wash treatment instead.
- **Don't** glow, gradient, or glass a data table, working card, or tool panel; those materials belong to the frame, not the instrument.
