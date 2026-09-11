"""NX-290 — retragerea unui endpoint web, după mecanismul standard (RFC 9745 + RFC 8594).

De ce un modul și nu două headere puse la mână în `app.py`: o retragere are patru afirmații care
trebuie să spună ACELAȘI lucru — headerul de pe răspunsul care încă merge, corpul lui `410` de
după flip, documentul către care trimite `Link`, și raportul care decide dacă ștergerea e sigură.
Ținute separat, ele divergează exact în momentul în care contează (cineva mută data de sunset în
doc și uită headerul). Aici sunt UN tabel, iar restul se derivă din el.

Ce e standardul, pe scurt:

  • `Deprecation` (RFC 9745) — un sf-date: `@<epoch>`. Momentul în care ruta a fost DECLARATĂ
    depășită. Trecut sau prezent = deja depășită; viitor = anunț.
  • `Sunset` (RFC 8594) — un HTTP-date (IMF-fixdate). Momentul de la care serverul își rezervă
    dreptul să refuze. NU e o promisiune că refuză automat la secunda aia.
  • `Link; rel="deprecation"` — unde scrie DE CE și ce se folosește în loc.
    `rel="successor-version"` — succesorul, ca să nu fie nevoie de arheologie.

Două decizii deliberate, ambele în răspăr cu implementările naive:

1. **Ceasul nu schimbă comportamentul.** O rută nu începe să dea `410` fiindcă a trecut data.
   Refuzul e o decizie explicită (`web_legacy_async_enabled=False`), reversibilă dintr-o variabilă
   de mediu. Altfel un răspuns ar depinde de când e citit, exact ce interzice NX-240 pentru
   proiecție, din același motiv: reproductibilitate. Data de sunset e o PROMISIUNE publicată, iar
   raportul spune dacă a trecut; codul nu o execută singur.

2. **Poarta stă DUPĂ autentificare.** Un apelant fără sesiune validă primește în continuare `403`,
   nu `410`: retragerea se anunță clienților legitimi, nu suprafeței de scanare.

Modulul e PUR (zero I/O, zero ceas propriu — `now` se injectează). Emiterea contorului stă în
`app.py`, unde există tenantul și providerul de DB.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import format_datetime

DOC_URL = "https://github.com/AdiBoaru/Sales-Ass/blob/main/docs/WEB-TRANSPORT-CONSOLIDATION.md"


@dataclass(frozen=True, slots=True)
class DeprecatedRoute:
    """O rută retrasă. `path` e id-ul folosit și în contor (vocabular ÎNCHIS, low-cardinality)."""

    path: str
    deprecated_at: datetime
    sunset_at: datetime
    successor: str
    reason: str


# Momentul anunțului și data de sunset sunt CONSTANTE, nu calculate la import: o dată derivată din
# `now()` s-ar muta la fiecare deploy, iar promisiunea ar fi mereu „peste 28 de zile de acum".
_DEPRECATED_ON = datetime(2026, 9, 10, tzinfo=UTC)
_SUNSET_ON = datetime(2026, 10, 8, 23, 59, 59, tzinfo=UTC)

# Transportul async v1. Ambele rute formează UN drum: `/web/messages` pune envelope pe stream,
# `/web/stream` livrează ce publică dispatcher-ul. Nu se retrage una fără cealaltă — jumătate de
# drum ar însemna mesaje acceptate care nu mai au pe unde ieși, adică tăcere (P6).
LEGACY_ASYNC_ROUTES: tuple[DeprecatedRoute, ...] = (
    DeprecatedRoute(
        path="/web/messages",
        deprecated_at=_DEPRECATED_ON,
        sunset_at=_SUNSET_ON,
        successor="/web/v2/turns",
        reason="fire_and_forget_delivery",
    ),
    DeprecatedRoute(
        path="/web/stream",
        deprecated_at=_DEPRECATED_ON,
        sunset_at=_SUNSET_ON,
        successor="/web/v2/turns/{turn_id}/events",
        reason="fire_and_forget_delivery",
    ),
)

_BY_PATH: dict[str, DeprecatedRoute] = {r.path: r for r in LEGACY_ASYNC_ROUTES}


def route(path: str) -> DeprecatedRoute:
    """Intrarea din registru. `KeyError` intenționat: o rută necunoscută e un bug de apelant, nu
    o stare de rulare — mai bine cade la primul request decât să tacă și să nu anunțe nimic."""
    return _BY_PATH[path]


def deprecation_headers(entry: DeprecatedRoute) -> dict[str, str]:
    """Headerele standard pentru un răspuns care ÎNCĂ funcționează.

    `Deprecation` e sf-date (RFC 9745 §2): `@` + secunde epoch, fără zecimale. `Sunset` e HTTP-date
    (RFC 8594 §3), adică IMF-fixdate — `format_datetime(..., usegmt=True)` e forma canonică din
    stdlib, cu `GMT` literal, nu `+0000` (un `+0000` e sintactic valid ca date, dar NU e IMF-fixdate
    și un client strict îl respinge)."""
    return {
        "Deprecation": f"@{int(entry.deprecated_at.timestamp())}",
        "Sunset": format_datetime(entry.sunset_at, usegmt=True),
        "Link": (
            f'<{DOC_URL}>; rel="deprecation"; type="text/markdown", '
            f'<{entry.successor}>; rel="successor-version"'
        ),
    }


def gone_headers(entry: DeprecatedRoute) -> dict[str, str]:
    """Headerele pentru `410`. Aceleași plus `Cache-Control: no-store`: un `410` cache-uit de un
    proxy ar supraviețui unui rollback al flagului, adică ar transforma o decizie reversibilă în
    una care nu se mai poate lua înapoi de pe server."""
    return {**deprecation_headers(entry), "Cache-Control": "no-store"}


def gone_detail(entry: DeprecatedRoute) -> dict[str, str]:
    """Corpul lui `410`, citibil de mașină. Un client care primește asta știe DE CE a picat și CE
    să cheme, fără să deschidă documentația — singurul motiv pentru care `410` e mai bun decât
    `404` pe o rută retrasă."""
    return {
        "error": "gone",
        "route": entry.path,
        "reason": entry.reason,
        "successor": entry.successor,
        "sunset": format_datetime(entry.sunset_at, usegmt=True),
        "doc": DOC_URL,
    }


def sunset_passed(entry: DeprecatedRoute, now: datetime) -> bool:
    """A trecut data promisă? Folosit de RAPORT, nu de calea fierbinte (vezi decizia 1 din antet).
    `now` e injectat ca să fie testabil fără să atingi ceasul procesului."""
    return now >= entry.sunset_at
