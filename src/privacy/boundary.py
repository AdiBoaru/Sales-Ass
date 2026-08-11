"""NX-230 — frontiera propriu-zisă: un mesaj brut intră, o pereche (raw request-scoped, safe
persistabil) iese. Pur: fără DB, fără LLM, fără I/O.

**Un singur loc.** Toate scrierile durabile pleacă de la `SafeInbound`. Dacă apare o a doua funcție
care „mai redactează puțin" pe undeva prin pipeline, politica are două surse de adevăr și una dintre
ele va rămâne în urmă — exact cum s-a întâmplat cu cele cinci regexuri de telefon pe care cardul
ăsta le-a consolidat.

**Fail-safe, nu fail-open.** Dacă detectorul crapă sau depășește bugetul de timp, NU persistăm
textul brut „ca să nu pierdem mesajul". Persistăm un placeholder marcat `degraded=True`. Un mesaj
pierdut e o pagubă de produs; un PII scris pe disc pentru că regexul a avut o zi proastă e o pagubă
de conformitate, și doar una dintre ele se poate repara retroactiv.
"""

from __future__ import annotations

import logging

from src.privacy.contracts import RawInbound, RawText, SafeInbound
from src.privacy.detectors import redact
from src.privacy.policy import profile

log = logging.getLogger(__name__)

# Text pus în locul mesajului când detectorul a eșuat. Nu conține nimic din original — nici măcar
# lungimea, care pentru un text scurt e ea însăși un semnal.
_DEGRADED_PLACEHOLDER = "[conținut neprocesat]"

# Peste atâtea caractere nu mai rulăm detectoarele pe tot textul: regexurile cu backtracking pe un
# input ostil de sute de KB sunt un DoS. Restul se taie și se marchează degradat — cerința de body
# cap există deja la margine (NX-120), asta e plasa de dedesubt.
_MAX_SCAN_CHARS = 64_000


def make_safe(text: str | None, *, sink: str = "persist") -> SafeInbound:
    """Text brut → `SafeInbound` pentru sink-ul cerut. Nu ridică niciodată.

    Orice excepție din detectoare devine rezultat degradat, nu propagare: frontiera de privacy nu
    are voie să fie ea însăși cauza pentru care un tur eșuează.
    """
    if not text:
        return SafeInbound(text="")
    try:
        if len(text) > _MAX_SCAN_CHARS:
            log.warning("privacy_guard_failed stage=make_safe reason=oversize")
            return SafeInbound(text=_DEGRADED_PLACEHOLDER, degraded=True)
        safe_text, counts = redact(text, categories=profile(sink))
    except Exception:  # noqa: BLE001 — vezi docstring: fail-safe, niciodată fail-open
        # Fără `log.exception`: traceback-ul ar putea conține fragmente din text.
        log.warning("privacy_guard_failed stage=make_safe reason=detector_error")
        return SafeInbound(text=_DEGRADED_PLACEHOLDER, degraded=True)
    return SafeInbound(
        text=safe_text,
        categories=tuple(sorted(counts)),
        counts=counts,
    )


def apply_boundary(
    body: str | None, *, content_type: str = "text"
) -> tuple[RawInbound, SafeInbound]:
    """Punctul de intrare al frontierei. Cheamă-l O SINGURĂ DATĂ per mesaj, cât mai devreme.

    Întoarce ambele forme deliberat:
      • `RawInbound` — pentru memoria turului (D6: agentul principal poate vedea query-ul brut).
        Poartă `RawText`, deci nu poate ajunge într-un log sau într-un JSON fără `.value` explicit.
      • `SafeInbound` — pentru absolut orice scriere durabilă.

    Apelantul nu poate încurca cele două: au tipuri diferite, iar cel brut nu se serializează.
    """
    return RawInbound(body=RawText(body or ""), content_type=content_type), make_safe(body)


def safe_for_telemetry(text: str | None) -> str:
    """Scurtătură pentru loguri/analytics/chei de cache: profilul cel mai strict."""
    return make_safe(text, sink="telemetry").text
