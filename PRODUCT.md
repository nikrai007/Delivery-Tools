# Product

## Register

product

## Platform

web

## Users

Two audiences share the same authenticated surface. The primary users are internal teams across the wider organization — engineers, team leads, and admins beyond the original delivery team — who log in to run operational delivery tasks: FK-safe Oracle rollbacks, query generation, release tracking, encrypt/decrypt, and the tools that follow. The secondary audience is external clients who are given accounts to use the same tools. Both arrive in a task, often a risky or repetitive one, and need to complete it themselves without escalating to whoever built the platform. Because strangers and outside clients are in scope, the interface has to teach itself and stay trustworthy to someone who has never seen it before.

## Product Purpose

Delivery Toolbox is a self-hosted, multi-tool platform: one Flask process, one login, one shared design system, and a growing set of independent tools that each live in their own space and can be added without disturbing the rest. AutoBackupRevert — an FK-safe Oracle rollback-script generator — is the first live tool; team management, query generation, release tracking, and more follow the same pattern. Success is measured by adoption growth: more tools shipped and more teams and clients onboarded over time. The platform earns that growth by making each new tool feel like a first-class part of one coherent product rather than a bolted-on script.

## Positioning

One login, many safe tools. A single self-hosted platform where independent delivery tools share one identity and one design language, and where a new tool can be added with zero impact on the others. No other option gives this audience a unified, self-hostable home for their operational tooling.

## Brand Personality

Modern and premium. The platform should feel polished, current, and quietly impressive — software that signals quality to a client evaluating it and confidence to an internal user trusting it with production data. The voice is precise and self-assured, never casual or cute. Craft is visible in the details (the aurora-and-glass auth surfaces, considered motion, consistent components), but it always serves the task rather than showing off. Premium here means restraint and finish, not decoration.

## Anti-references

Deliberately avoid all of the following:
- **Generic Bootstrap admin** — off-the-shelf boxy cards, stock icons, and indistinct layouts that read as a template rather than a product.
- **Cluttered legacy enterprise** — the dense, grey, cramped, intimidating feel of dated internal tools (Oracle Forms, old SAP).
- **Playful consumer/startup** — gradients everywhere, big emoji, bouncy mascots, or anything too casual for production operations and client trust.
- **Sterile and personality-free** — so plain it feels unfinished or unloved, with no craft and nothing memorable.

The line to walk: distinctive and finished, but never flippant; serious, but never drab.

## Design Principles

- **One system, many tools.** Every tool inherits the shared identity, components, and motion vocabulary. Cross-tool consistency is the product; a new tool should be recognizable as part of the whole on first glance.
- **Zero-impact extensibility.** Adding a tool never degrades or restyles the others. The design system is the contract that keeps growth clean.
- **Premium through restraint.** Polish and finish signal trustworthiness to clients and internal users alike. Reach for craft in the details, not decoration for its own sake.
- **Teach on arrival.** Because outside clients and unfamiliar teams self-serve, screens explain themselves — clear affordances, meaningful empty and error states, no tribal knowledge required.
- **Safe to self-serve.** These tools perform risky operational work; guardrails, reversibility, and audit trails are visible and reassuring, so users trust the platform enough to act without escalating.

## Accessibility & Inclusion

Aim high and broadly adoptable, since a varied internal-and-external audience depends on it. Target WCAG 2.1 AA as the baseline: body text at ≥4.5:1 contrast, complete keyboard operability across every tool, and visible focus states. Honor `prefers-reduced-motion` on the aurora, glass shimmer, and breathing-glow effects with calm crossfade or instant alternatives. Keep state indicators (error, warning, success) distinguishable beyond color alone for color-blind users. The bar is that anyone in the intended audience can complete their task regardless of ability or device.
