"""Căutarea și rankingul agentului unic (NX-404): ce produse vede agentul și în ce ordine.

Până la NX-404 agentul chema direct `search_products_lexical` cu nevoile ca filtre DURE, deci:
- un produs fără atributul cerut ieșea (pe SOLE, 173 din 287 de creme de față n-au `skin_type`):
  UNKNOWN ≠ MISMATCH (D7) era încălcat exact pe calea principală;
- scara de text se oprea la prima treaptă cu orice rezultat, chiar cu un singur produs;
- ordinea era rangul textului, apoi `p.id`: ratingul, recenziile și stocul nu contau;
- nuanțele aceleiași familii ocupau locuri separate (6 din 7 la un SPF).
Măsurat pe cele 60 de căutări reale ale agentului din 9-10 oct: în 35 agentul vedea sub 4 produse
diferite, iar 8 rânduri contraziceau nevoia spusă.

Acum (`fetch_candidates` + `rank_candidates`):
1. nevoile spuse rămân primele: se aduc și produsele care le POTRIVESC (filtru dur, ca înainte),
   și cele de pe același raft și tip FĂRĂ filtrul de nevoi, apoi, dacă tot sunt puține, setul
   filtrelor fără text (treapta `filters_only`, ordonată după text);
2. starea nevoii pe fiecare produs (`need_status`): potrivește / necunoscut (n-are atributul) /
   nu potrivește; nepotrivitul iese, potrivirea stă ÎNAINTEA necunoscutului (o pondere în scor
   punea necunoscutele peste potriviri: 182 → 147 pe replay), iar orice produs în stoc stă
   înaintea unui epuizat;
3. în fiecare grup ordinea e rankingul existent al producției (`fusion.blended_rerank`: relevanța
   textului, ratingul ajustat la recenzii, stocul, reducerea, nevoile; ponderile pachetului),
   deci rankingul are UN proprietar pe ambele căi;
4. o familie (același nume scurt) e un rând, cu numărul de variante.
Rezultatul pe aceleași 60 de căutări: sub 4 produse diferite în 8 (din 35), 0 rânduri care
contrazic nevoia (din 8), potrivirile neatinse (182 → 184), +30-50 ms pe căutare."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from typing import Any

#: Câți candidați caută agentul înainte să se oprească din scara de text.
MIN_CANDIDATES = 20
#: Câte rânduri aduce fiecare interogare (cea cu nevoile dure și cea fără ele).
POOL = 60

_NEED_TIER = {"match": 0, "n/a": 0, "unknown": 1}


def split_needs(needs: Sequence[str]) -> dict[str, list[str]]:
    """`["skin_type:oily", "concerns:acne"]` → `{"skin_type": ["oily"], "concerns": ["acne"]}`."""
    out: dict[str, list[str]] = {}
    for need in needs:
        dim, _sep, value = str(need).partition(":")
        if dim and value:
            out.setdefault(dim, []).append(value)
    return out


def need_status(row: Mapping[str, Any], needs: Mapping[str, Sequence[str]]) -> str:
    """Starea nevoilor spuse pe un produs. PUR.

    `n/a` = nicio nevoie; `match` = fiecare dimensiune are una dintre valorile cerute; `mismatch` =
    o dimensiune are atributul, cu alte valori; `unknown` = măcar o dimensiune lipsește de pe
    produs, iar celelalte potrivesc. Valorile aceleiași dimensiuni sunt alternative (ca în SQL)."""
    if not needs:
        return "n/a"
    attrs = row.get("attributes") or {}
    if not isinstance(attrs, Mapping):
        attrs = {}
    status = "match"
    for dim, wanted in needs.items():
        if not wanted:
            continue
        raw = attrs.get(dim)
        if raw in (None, "", [], {}):
            status = "unknown"
            continue
        values = {_text(v) for v in raw} if isinstance(raw, list) else {_text(raw)}
        if not values & {_text(w) for w in wanted}:
            return "mismatch"
    return status


def _text(value: Any) -> str:
    """O valoare de atribut ca textul pe care îl compară SQL-ul (`->>`): `true`/`false` pentru un
    boolean (Python ar da `True`), un număr întreg fără zecimale."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def family_key(row: Mapping[str, Any]) -> str:
    """Familia unui produs: numele scurt, fără variantă (nuanța, gramajul)."""
    from src.catalog.render_text import display_name  # noqa: PLC0415

    return display_name(str(row.get("name") or "")).casefold().strip()


def rank_candidates(
    rows: Sequence[dict[str, Any]],
    needs: Mapping[str, Sequence[str]],
    *,
    sort: str = "relevance",
    weights: Mapping[str, float] | None = None,
) -> list[dict[str, Any]]:
    """Ordinea în care agentul vede candidații. PUR (în afara rankingului refolosit).

    Pe `relevance`: rankingul producției (`blended_rerank`, cu poziția din căutare ca relevanță și
    epuizatele coborâte), apoi stocul și grupul nevoii (stabil). Pe o sortare cerută, cheia
    cerută în fiecare grup: candidații vin din interogări diferite, deci concatenarea lor nu e
    ordonată (recenzia NX-404: «price_asc» ieșea [80, 90, 100, 4, 5]). `weights=None` ⇒ rankingul
    de dinainte de blend (`deterministic_rerank`), ca pe calea veche cu kill-switch-ul stins.
    Nepotrivitul iese; o familie e adunată de `group_families`, după siguranță."""
    from src.db.queries.fusion import (  # noqa: PLC0415
        _shrunk_rating,
        blended_rerank,
        demote_out_of_stock,
        deterministic_rerank,
        rrf_scores,
    )

    kept = [dict(r) for r in rows if need_status(r, needs) != "mismatch"]
    sort_keys = {
        "price_asc": lambda r: (_price(r), -_shrunk_rating(r)),
        "price_desc": lambda r: (-_price(r), -_shrunk_rating(r)),
        "rating_desc": lambda r: (-_shrunk_rating(r), _price(r)),
    }
    if sort in sort_keys:
        kept.sort(key=sort_keys[sort])
    elif sort == "relevance" and kept:
        scores = demote_out_of_stock(kept, rrf_scores(kept, []))
        concerns = list(needs.get("concerns") or [])
        prefer = {k: list(v) for k, v in needs.items()} or None
        if weights is None:
            kept = deterministic_rerank(kept, scores, concerns=concerns, prefer=prefer)
        else:
            kept = blended_rerank(
                kept, scores, weights=dict(weights), concerns=concerns, prefer=prefer
            )
    # Stocul, apoi grupul nevoii: orice produs în stoc stă înaintea unui epuizat, care nu se poate
    # cumpăra. Penalizarea din ranking (cinci locuri) nu ajungea când epuizatul venea primul la text
    # și cele în stoc din completare (turul 1 din `2789a469`: MEDICUBE epuizat pe locul 1, adică
    # exact produsul pomenit nechemat). Epuizatul rămâne în listă, găsibil când e cerut pe nume.
    kept.sort(key=lambda r: (not _in_stock(r), _NEED_TIER[need_status(r, needs)]))
    return kept


def _in_stock(row: Mapping[str, Any]) -> bool:
    return row.get("availability") in ("in_stock", "low_stock")


def _price(row: Mapping[str, Any]) -> float:
    value = row.get("price")
    return float(value) if isinstance(value, int | float) else float("inf")


def group_families(
    rows: Sequence[dict[str, Any]], limit: int
) -> list[tuple[dict[str, Any], list[dict[str, Any]]]]:
    """Primele `limit` familii, în ordinea listei: (reprezentantul, celelalte variante). PUR.
    Reprezentantul e cel mai bine clasat din familie; variantele rămân accesibile agentului (le
    poate compara, citi sau pune pe card), doar nu ocupă câte un rând fiecare."""
    groups: list[tuple[dict[str, Any], list[dict[str, Any]]]] = []
    index: dict[str, int] = {}
    for r in rows:
        key = family_key(r)
        if key and key in index:
            groups[index[key]][1].append(r)
            continue
        if len(groups) >= limit:
            continue
        if key:
            index[key] = len(groups)
        groups.append((r, []))
    return groups


async def fetch_candidates(
    db: Callable[[], Any],
    business_id: str,
    args: Mapping[str, Any],
    *,
    locale: str | None,
    parallel: bool = True,
    stats: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Candidații unei căutări a agentului, NEORDONAȚI încă (`rank_candidates` îi ordonează):
    potrivirile nevoilor, apoi raftul și tipul fără nevoi (scara de text obișnuită, deci treapta
    fiecărui rând e cea adevărată), apoi, sub `MIN_CANDIDATES`, setul filtrelor fără text. Fiecare
    rând apare o dată, în ordinea primei apariții.

    `db()` deschide un checkout SCURT tenant-scoped (`deps.db`, NX-231); `business_id` vine din
    server (P7). Primele două interogări sunt independente, deci rulează în PARALEL, fiecare pe
    conexiunea ei; `parallel=False` pe un provider cu o singură conexiune (`static_db`), care nu
    suportă două operații simultane. O primă variantă cerea raftul direct pe treapta „doar
    filtrele”: mai rapidă, dar eticheta „fără potrivire de text” ajungea pe TOATE rândurile, iar cu
    `SEARCH_FILTERS_ONLY_FALLBACK_ENABLED` stins căutarea întorcea nimic (recenzia NX-404).

    NX-409: cu nevoi și un subiect, a treia interogare (în paralel) aduce TOATE produsele care
    poartă nevoile pe aceleași filtre, fără text (`only_filters_step`, plafon `POOL`). Agentul
    căuta de 3-4 ori produse potrivite care nu existau (pe SOLE, 4 creme de față în stoc spun
    „ten gras”, iar prima căutare le avea pe toate), fiindcă nu știa câte sunt. Cu setul întreg,
    lista le conține pe toate, iar `stats["need_counted"]` spune că numărătoarea lor e completă
    (`stats["need_pool_full"]` = plafonul atins, deci „cel puțin”). Fără subiect nu rulează:
    setul unei nevoi singure e tot catalogul care o poartă (pe SOLE `oily` e și pe 38 de
    luciuri)."""
    import asyncio  # noqa: PLC0415

    from src.db.queries.catalog import search_products_lexical  # noqa: PLC0415

    async def run(**kw: Any) -> list[dict[str, Any]]:
        async with db() as conn:
            return await search_products_lexical(conn, business_id, query, **common, **kw)

    needs = split_needs(args.get("needs") or [])
    types = list(args.get("product_types") or [])
    structural = {"product_type": types} if types else {}
    common = dict(
        category=args.get("category"),
        brand=args.get("brand"),
        price_max=args.get("price_max"),
        sort_mode=args.get("sort") or "relevance",
        in_stock_only=bool(args.get("in_stock_only")),
        locale=locale,
        allow_filters_only=True,
        pool=POOL,
    )
    query = str(args.get("query") or "")
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()

    def add(batch: Sequence[dict[str, Any]]) -> None:
        for r in batch:
            pid = str(r.get("id"))
            if pid not in seen:
                seen.add(pid)
                rows.append(r)

    has_subject = bool(common["category"] or structural or common["brand"])
    first = [{"facet_filters": {**structural, **needs}}] if needs else []
    first.append({"facet_filters": structural or None})
    counted = bool(needs) and has_subject
    if counted:
        first.append({"facet_filters": {**structural, **needs}, "only_filters_step": True})
    if parallel:
        batches = await asyncio.gather(*(run(**kw) for kw in first))
    else:
        batches = [await run(**kw) for kw in first]
    for batch in batches:
        add(batch)
    if stats is not None:
        stats["need_counted"] = counted
        stats["need_pool_full"] = counted and len(batches[-1]) >= POOL
    if len(rows) < MIN_CANDIDATES and has_subject:
        add(await run(facet_filters=structural or None, only_filters_step=True))
    return rows
