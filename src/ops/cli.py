"""NX-248 — mărunțișuri comune uneltelor de linie de comandă.

Un singur lucru, dar unul care a costat deja timp: pe Windows, `sys.stdout` e cp1252 când e
redirecționat într-un pipe, iar TOATE mesajele noastre sunt în română. Un `print` cu „migrări"
crapă cu `UnicodeEncodeError` — nu în consolă (unde e UTF-8), ci exact atunci când altcineva
CITEȘTE ieșirea programatic: un harness care rulează drill-uri, un script de raport, CI-ul local.
Rezultatul e un exit code ≠ 0 care nu are nimic de-a face cu verificarea rulată, adică un fals
negativ în chiar instrumentul de măsură (aceeași lecție ca `eval_run` din memoria proiectului).

`scripts/migrate.py` avea deja linia asta în `main()`; aici o punem o singură dată, ca uneltele
noi să n-o mai uite.
"""

from __future__ import annotations

import sys


def enable_utf8_stdout() -> None:
    """Forțează UTF-8 pe stdout/stderr acolo unde platforma nu-l dă (Windows, pipe)."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8")
        except (ValueError, OSError):  # pragma: no cover — stream deja închis/înlocuit în teste
            pass
