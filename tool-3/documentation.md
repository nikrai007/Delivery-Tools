# Tool-3 (scaffold)

A working **placeholder** showing the minimum wiring a new Delivery Toolbox tool
needs. It is registered as the `tool3` blueprint at `/tools/tool-3/` and simply
redirects back to the hub until it's built.

## Layout

| Path | Role |
|---|---|
| `source-code/tool3_routes.py` | Blueprint `tool3` (placeholder route). |
| `dependencies/` | Tool-specific third-party/vendored deps. |
| `configuration/` | Tool-specific config. |

## Turning it into a real tool

1. Implement routes/logic in `source-code/` (split into more modules as needed).
2. Add a `tool-3/templates/` folder and point the blueprint's `template_folder`
   at it; extend the shared `base.html` for consistent chrome.
3. Register it in [../app.py](../app.py) — already done for this scaffold
   (`_CODE_DIRS` + `app.register_blueprint(tool3_bp)`).
4. Promote its card in
   [../landing-page/source-code/landing_routes.py](../landing-page/source-code/landing_routes.py)
   to `status="live"` with `endpoint="tool3.home"`.

Use the `auto-backup-revert-tool/` folder as the reference for a fully-built tool.
