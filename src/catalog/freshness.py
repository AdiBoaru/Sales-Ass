"""Cine decide dacă un fapt de catalog se judecă în timp: TENANTUL, nu o constantă globală.

Pragul de prospețime (`COMMERCE_FACTS_SLA_S`) e o variabilă de mediu, deci una singură pentru
toți clienții. Asta ține cât timp toți au același fel de catalog. În clipa în care un tenant e
alimentat de un feed live și altul e un snapshot importat o dată, aceeași cifră înseamnă două
lucruri diferite, iar unul dintre ei primește tăcut politica celuilalt.

Consecința măsurată pe `sole-ro` (2026-08-31), care a făcut modulul ăsta necesar: catalogul a
fost citit pe 27.08 și importat pe 28.08; la 24 de ore după import, `price` și `availability` au
devenit `stale` pe TOATE cele 2.758 de produse ⇒ prețul nu se mai afișează, iar `sellable`
răspunde `availability_stale` pe fiecare, deci zero butoane de coș. Nu se stricase nimic: pragul
global spunea „un fapt neconfirmat de 24h nu se rostește", iar catalogul nu avea cine să-l
reconfirme.

**Inversiunea pe care o repară.** `Fact.known` declară `stale` DOAR ce a fost verificat
(`verified_at is not None`) și a depășit pragul. Un catalog fără nicio urmă de verificare
(`synced_at` NULL) nu poate deveni stale, deci se afișează integral. Măsurat pe același produs:

    synced_at = import 28.08      → price stale,  availability stale,  CTA refuzat
    synced_at = NULL              → price known,  availability known,  CTA permis

Adică politica pedepsea mai tare tenantul care a ÎNREGISTRAT când și-a citit sursa decât pe cel
care n-a înregistrat nimic. Modulul ăsta nu relaxează nicio garanție: mută decizia de la o cifră
globală la o declarație explicită a tenantului, ca `synced_at` să poată rămâne adevărat fără să
stingă catalogul.

PUR: fără DB, fără ceas, fără config global citit pe ascuns — pragul implicit vine ca argument.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Final

#: Cheia din `businesses.settings` care poartă declarația.
SETTINGS_KEY: Final = "catalog_freshness"

#: Catalogul e alimentat de un sync care îl reconfruntă cu sursa. Faptele se judecă în timp.
MODE_SYNCED: Final = "synced"

#: Catalogul e o fotografie importată o dată, fără proces care s-o reîmprospăteze. Faptele NU se
#: judecă în timp, fiindcă n-ar exista niciodată un „proaspăt" în care să intre înapoi.
MODE_STATIC: Final = "static_snapshot"

MODES: Final = frozenset({MODE_SYNCED, MODE_STATIC})


def facts_sla_s(settings: Mapping[str, Any] | None, *, default: int) -> int | None:
    """Pragul de prospețime al faptelor comerciale pentru acest tenant.

    `None` = faptele nu se judecă în timp (catalog declarat static). Un întreg = pragul, în
    secunde. Orice declarație lipsă, necunoscută sau malformată cade pe `default`: necunoscutul
    trebuie să ducă la politica CONSERVATOARE (judecăm, deci putem omite), nu la cea permisivă —
    altfel o greșeală de tastare într-un jsonb ar transforma tăcut un catalog viu în unul
    scutit de verificare.
    """
    declared = (settings or {}).get(SETTINGS_KEY)
    if not isinstance(declared, Mapping):
        return default
    mode = declared.get("mode")
    if mode == MODE_STATIC:
        return None
    if mode == MODE_SYNCED:
        raw = declared.get("sla_s")
        # `bool` e subclasă de `int`: `True` ar trece ca 1 secundă și ar face totul stale.
        if isinstance(raw, int) and not isinstance(raw, bool) and raw > 0:
            return raw
        return default
    return default


def is_static(settings: Mapping[str, Any] | None) -> bool:
    """Tenantul și-a declarat catalogul drept snapshot? Pentru rapoarte și diagnostic: o scutire
    permanentă de la verificare trebuie să fie VIZIBILĂ, nu dedusă din faptul că nimic nu e stale.
    """
    declared = (settings or {}).get(SETTINGS_KEY)
    return isinstance(declared, Mapping) and declared.get("mode") == MODE_STATIC


__all__ = ["MODES", "MODE_STATIC", "MODE_SYNCED", "SETTINGS_KEY", "facts_sla_s", "is_static"]
