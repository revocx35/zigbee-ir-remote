# AGENTS.md

Notes for AI agents working on this repository. The full notes (release workflow, how to test without a
real Home Assistant, hard-won lessons, behaviour decisions agreed with the owner) are in
[CLAUDE.md](CLAUDE.md); the code structure, data model, MQTT topics, HTTP API and agent API (MCP) are in
[ARCHITECTURE.md](ARCHITECTURE.md). Read both before changing anything.

Short version: one add-on in `zigbee_ir_remote/` (Python `app/main.py` with aiohttp only, vanilla JS in
`app/static/`, no build step). Pushing to `main` is a release, so bump `version:` in `config.yaml` and
`APP_VERSION` in `main.py`, and update `CHANGELOG.md` and `DOCS.md`. Check with
`python3 -m py_compile zigbee_ir_remote/app/main.py` and `node --check zigbee_ir_remote/app/static/app.js`.
Never commit `data/`, tokens or certificates.
