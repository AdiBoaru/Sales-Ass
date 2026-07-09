"""NX-160 — orchestrarea PURĂ a memoriei v2 (capture broad → classify → canonicalize).

Leagă `memory_safety.classify` + `canonicalize.resolve_canonical` într-un singur pas determinist:
primește candidații LIBERI extrași de model, întoarce rândurile de upsert (îmbogățite cu
`raw_key`/`canonical_key`/`memory_key`/`safety_class`/`visibility`) + contoare pentru analytics.
PUR (fără DB/LLM) — processorul doar apelează `process_facts` și scrie rezultatul.

Politica de vizibilitate:
  • drop      → PII/financial: NU se persistă deloc (nici măcar ca semnal).
  • candidate → sensibil (ex. condiție medicală): se persistă, dar NU ajunge în prompt.
  • inject    → sigur: se persistă și poate fi injectat de `facts_block`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from src.worker.canonicalize import memory_key, resolve_canonical
from src.worker.memory_safety import classify

if TYPE_CHECKING:
    from src.domain.pack import DomainPack

# Igienă: o valoare de fapt e un scalar/listă SCURTĂ (nu eseuri). Aliniat cu profile._MAX_VALUE_LEN.
_MAX_VALUE_LEN = 200


@dataclass
class ProcessedFacts:
    """Rezultatul procesării: rândurile de scris (doar visibility ≠ drop) + contoare pt analytics
    (chei/numere, ZERO valori — P12)."""

    rows: list[dict[str, Any]] = field(default_factory=list)
    dropped: int = 0  # PII/financial aruncate (nu se persistă)
    candidate: int = 0  # sensibile stocate dar neinjectabile
    injectable: int = 0  # sigure, injectabile
    canonicalized: int = 0  # câte au primit canonical_key


def _too_long(value: Any) -> bool:
    return isinstance(value, str) and len(value) > _MAX_VALUE_LEN


def process_facts(
    candidates: list[dict[str, Any]],
    pack: DomainPack | None,
    *,
    source_message_id: str | None = None,
) -> ProcessedFacts:
    """Procesează candidații LIBERI ai extractorului → `ProcessedFacts`. Pentru fiecare:
    1. `raw_key` = cheia liberă a modelului (fallback `fact_type` backcompat); valori goale/prea
       lungi → sărite;
    2. `canonical_key` = `resolve_canonical(raw_key, pack)` (None dacă nu mapăm sigur);
    3. `SafetyVerdict` = `classify(...)` → `visibility` (drop = sărit, nepersistat);
    4. dedupe pe `memory_key` în cadrul turului, păstrând confidence maxim.
    """
    out = ProcessedFacts()
    best: dict[str, dict[str, Any]] = {}
    for c in candidates:
        raw_key = c.get("raw_key") or c.get("fact_type")
        value = c.get("fact_value")
        if not raw_key or value in (None, "", [], {}) or _too_long(value):
            continue
        canonical_key = resolve_canonical(str(raw_key), pack)
        if canonical_key:
            out.canonicalized += 1
        verdict = classify(str(raw_key), canonical_key, value)
        if verdict.visibility == "drop":
            out.dropped += 1
            continue
        if verdict.visibility == "candidate":
            out.candidate += 1
        else:
            out.injectable += 1
        conf = c.get("confidence")
        conf = 0.5 if conf is None else conf
        mkey = memory_key(str(raw_key), canonical_key)
        row = {
            "raw_key": str(raw_key).strip().lower(),
            "canonical_key": canonical_key,
            "memory_key": mkey,
            "fact_value": value,
            "confidence": conf,
            "safety_class": verdict.safety_class,
            "visibility": verdict.visibility,
            "source_message_id": source_message_id,
        }
        prev = best.get(mkey)
        # dedupe în tur: o observație cu confidence mai mare câștigă valoarea (ca la persistare).
        if prev is None or float(conf) >= float(prev["confidence"]):
            best[mkey] = row
    out.rows = list(best.values())
    return out
