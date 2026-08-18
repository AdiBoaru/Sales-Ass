"""NX-247 — harnessul E2E Stage 1 (test-only).

Nimic din acest pachet nu ajunge în imaginea de producție: `Dockerfile` copiază `src/`, `scripts/`
și `docs/*.sql`, niciodată `tests/`. Poarta nu e însă o convenție de layout — `stage1_app.py`
refuză structural să se construiască în afara `ENV=test` și în afara loopbackului. Vezi
`docs/STAGE1-WEB-E2E.md`.
"""
