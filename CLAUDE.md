# Heat Pump Optimizer — Development Rules

## Python Command
ALWAYS use `python3`, never bare `python`. macOS may not have `python` on PATH.

## Version Management
Two files must stay in sync — ALWAYS update both when bumping:
- `custom_components/heatpump_optimizer/const.py` line 4: `VERSION = "x.y.z"`
- `custom_components/heatpump_optimizer/manifest.json`: `"version": "x.y.z"`

Panel.js cache bust uses VERSION automatically via `__init__.py`.

## Translation Sync
`strings.json` and `translations/en.json` must have identical content.
After editing `strings.json`, ALWAYS copy it to `translations/en.json`.

## Config Flow Migration
`config_flow.py` `VERSION = N` requires migration handlers in `__init__.py`
`async_migrate_entry()` for all versions 1..N-1. When bumping `config_flow.py`
VERSION, add a migration block.

## Testing
- Run `python3 -m pytest tests/ -x -q` before committing
- Run `python3 tools/validate.py` for structural checks (versions, translations, imports)
- 720 tests run in ~3 seconds — always run the full suite, never skip failures

## Import Rules
- `coordinator.py` imports all 44+ modules — it is the import bottleneck
- Every new `.py` module must be importable via the coordinator chain
- Use relative imports (`from .xxx import`) within the integration

## Release Process
Use the `/release` command. Never manually push tags without running full validation.
The automated hooks run `tools/validate.py` before every commit and add pytest before
every tag — do not bypass them.

## Architecture
- 44+ Python modules across 5 packages: `adapters/`, `controllers/`, `engine/`, `learning/`, root
- All tests mock HA via `conftest.py` — pytest runs without Home Assistant installed
- Frontend: `frontend/panel.js` — custom web component registered as sidebar panel
