"""NX-275 felia 6 — RETRIEVAL SPECULATIV: căutarea se face înainte de primul apel de model.

Măsurat pe `reports/nx239/drive.json`: **15 din 16 ture au exact o rundă de tool**. Adică forma
tipică a unui tur de recomandare e „apelul 1 cere `search_products`, apelul 2 produce planul", iar
apelul 1 nu face altceva decât să emită un tool call de vreo 120 de tokeni. Costă un prefix întreg
și un round-trip complet ca să afle ceva ce codul putea decide singur: că un mesaj de recomandare
are nevoie de o căutare.

**Ideea:** pe profilul `recommend`, codul rulează căutarea ÎNAINTE de apelul 1, cu argumente
deterministe, și SEEDUIEȘTE bucla cu perechea (assistant tool_call, tool result) — ca și cum
modelul ar fi cerut-o. Apelul 1 devine, în cazul bun, apelul FINAL.

**Ce NU e.** Nu e un al doilea creier care decide ce să caute: argumentele sunt mesajul BRUT plus,
cel mult, un buget pe care clientul chiar l-a rostit (NX-251). Nu e o scurtătură care ocolește
porțile: `execute` e ACELAȘI `_PortedExecute`, deci trece prin portul NX-238, prin safety (NX-173)
și prin admission/buget (NX-241). Și nu e obligatoriu pentru model: `search_products` rămâne în
toolset, deci modelul poate căuta din nou cu filtre (`concerns`, `category`, `price_max`). Calitatea
nu poate scădea sub cea de azi — poate doar costa un apel în plus când seed-ul e nepotrivit.

**De aceea felia are prag, nu doar flag.** Un seed nimerit scoate un apel; un seed ratat adaugă
unul. Pragul de rentabilitate calculat în design: **43% hit**. Sub el, felia rămâne stinsă — și
`speculative_retrieval{outcome}` e chiar instrumentul care spune pe ce parte a pragului suntem.
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from src.catalog.query_terms import content_terms
from src.conversation.needs import corroborated_by

log = logging.getLogger(__name__)

__all__ = ["SEED_TOOL", "seed_call_id", "seed_messages", "skip_reason"]

#: Unealta pe care o speculăm. Una singură, și e o decizie: `search_products` e singurul tool pe
#: care îl putem apela din mesajul brut fără să ghicim o ancoră (un `get_product_details` ar cere
#: un `product_id` pe care doar modelul sau `reference_resolver` îl au).
SEED_TOOL = "search_products"

#: Mărcile unei vederi care NU e un rezultat util. Un seed care a fost refuzat (safety) sau a picat
#: pe o dependență nu are ce să semene în conversație: l-am prezenta modelului ca pe o căutare pe
#: care „a făcut-o" și care n-a găsit nimic, ceea ce e o minciună despre catalog.
_UNUSABLE_MARKERS = ("dependency_unavailable", "safety_excluded")


def seed_call_id(turn_id: str) -> str:
    """Id de tool call DETERMINIST, derivat din `turn_id`.

    Nu `uuid4()`: un tur reluat (reclaim, NX-233) trebuie să reconstruiască ACELEAȘI mesaje, altfel
    prefixul conversației diferă de la o încercare la alta și prompt cachingul (felia 3) nu se mai
    prinde niciodată pe reluări. Prefixul `spec_` îl face recunoscibil într-un trace."""
    return "spec_" + hashlib.sha256(f"spec:{turn_id}".encode()).hexdigest()[:16]


def skip_reason(
    *,
    speculative_profile: bool,
    has_action: bool,
    has_anchor: bool,
    is_pagination: bool,
    message: str,
    locale: str,
) -> str | None:
    """De ce NU speculăm, sau None dacă e în regulă. PURĂ.

    Fiecare motiv are o consecință concretă dacă îl ignori:
      • alt profil decât `recommend` → un tur exact ar plăti o căutare pe care n-o va folosi;
      • acțiune opacă → turul pornește dintr-un buton, nu dintr-o nevoie de căutat;
      • ancoră rezolvată → produsul e deja identificat. Nu cauți, ceri detalii despre el;
      • paginare → pool-ul sesiunii există deja, iar o căutare nouă l-ar înlocui tăcut (exact
        regresia NX-251 pe `show_more`);
      • mesaj fără termeni de conținut („da", „ok", „mersi") → căutarea ar returna zgomot, iar
        zgomotul seedat e mai rău decât absența: modelul l-ar lua drept candidați.
    """
    if not speculative_profile:
        return "profile"
    if has_action:
        return "action"
    if has_anchor:
        return "anchor"
    if is_pagination:
        return "pagination"
    if not content_terms(message or "", locale):
        return "no_content_terms"
    return None


def _seed_args(message: str, *, locale: str) -> dict[str, Any]:
    """Argumentele căutării speculative. DETERMINISTE, din mesajul brut.

    Singurul filtru pe care îl punem e bugetul, și doar dacă a fost ROSTIT (`corroborated_by`,
    NX-251): un buget dedus ar restrânge candidații pe baza unei presupuneri, exact ce D7 interzice
    pentru constrângerile care nu vin de la client. Restul filtrelor rămân ale MODELULUI, care
    poate re-căuta cu `concerns`/`category` dacă seed-ul nu acoperă nevoia.
    """
    return {
        "query": message,
        "price_max": _spoken_budget(message),
        "category": None,
        "brand": None,
        "concerns": None,
        "features": None,
        "sort_mode": "relevance",
        "in_stock_only": False,
        "limit": 6,
        "product_name": None,
        "variant_label": None,
    }


def _spoken_budget(message: str) -> float | None:
    """Bugetul DOAR dacă apare literal în mesaj. Altfel None (fără filtru de preț).

    Reutilizează `corroborated_by` din NX-251 — aceeași regulă care decide dacă un fapt e al
    clientului sau al modelului. Aici e folosită la fel: filtrăm pe o sumă doar când clientul a
    rostit-o."""
    import re  # noqa: PLC0415 — folosit doar aici, pe o cale rece

    for raw in re.findall(r"\b\d{2,5}\b", message or ""):
        if corroborated_by(message, raw):
            return float(raw)
    return None


async def seed_messages(
    *,
    turn_id: str,
    message: str,
    locale: str,
    execute: Callable[[str, dict[str, Any]], Awaitable[str]],
) -> tuple[list[dict[str, Any]] | None, str]:
    """Rulează căutarea și întoarce `(mesajele de seed, outcome)`.

    `outcome` e vocabular ÎNCHIS pentru telemetrie: `seeded` | `unusable` | `failed`. Orice eșec
    întoarce `(None, ...)` — un seed care nu se poate face NU are voie să rupă turul (P6), doar îl
    lasă să meargă exact ca azi.
    """
    args = _seed_args(message, locale=locale)
    try:
        view = await execute(SEED_TOOL, args)
    except Exception as e:  # noqa: BLE001 — o optimizare nu are voie să rupă un tur (P6)
        log.warning("speculative: căutarea a eșuat (%s) — turul continuă normal", type(e).__name__)
        return None, "failed"
    if not view or any(m in view for m in _UNUSABLE_MARKERS):
        return None, "unusable"

    call_id = seed_call_id(turn_id)
    return [
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": call_id,
                    "type": "function",
                    "function": {
                        "name": SEED_TOOL,
                        # `sort_keys` nu e cosmetică: aceiași octeți la fiecare reluare a turului,
                        # deci prefixul conversației rămâne cache-uibil.
                        "arguments": json.dumps(args, sort_keys=True, ensure_ascii=False),
                    },
                }
            ],
        },
        {"role": "tool", "tool_call_id": call_id, "content": view},
    ], "seeded"
