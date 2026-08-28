"""Query-uri pe catalog (read pentru bot).

Principiul 7: fiecare query are EXPLICIT `where business_id = $1` (mecanism
primar). RLS (rolul bot_runtime + app.business_id din tenant_conn) e plasa.

search_products aici e versiunea cu FILTRE SQL (categorie, brand, preț, text).
Ranking-ul SEMANTIC (embedding <=> query pe subsetul filtrat) se adaugă după ce
există product_embeddings (job de embed). Vezi schema_reference.md.
"""

import json
from collections.abc import Mapping, Sequence
from typing import Any

import asyncpg

from src.catalog.query_terms import content_terms, fold, relaxed_query, strict_query
from src.catalog.vocabulary import servable_count_sql
from src.config import get_settings

# NX-191 — FEREASTRA promoției. `sale_price` fără verificarea ferestrei = minciună comercială:
# o promoție încheiată ar continua să se afișeze ca preț curent, iar validatorul (stagiul 8) ar
# confirma-o, pentru că vede același preț greșit. Fereastra deschisă (ambele NULL) = promoție
# permanentă — comportamentul de dinainte de card, deci rândurile vechi rămân valide.
# `current_date` e evaluat de Postgres în fusul sesiunii; suficient pentru o graniță pe ZI.
_SALE_WINDOW_OK = (
    "(p.sale_start is null or p.sale_start <= current_date)"
    " and (p.sale_end is null or p.sale_end >= current_date)"
)
# Promoția e ACTIVĂ: există, e mai mică decât prețul de listă ȘI e în fereastră.
_SALE_ACTIVE = f"(p.sale_price is not null and p.sale_price < p.price and {_SALE_WINDOW_OK})"
# Prețul de produs ținând cont de fereastră (fallback-ul când nu există variante).
_PRODUCT_PRICE = f"(case when {_SALE_ACTIVE} then p.sale_price else p.price end)"

# Prețul REAL e pe variantă (vezi T037): fiecare produs are variante cu sale_price
# propriu, de obicei mai mic decât products.price. Sursăm min(variant) cu fallback
# la products. Validatorul de preț trebuie să vadă același preț ca clientul.
_EFFECTIVE_PRICE = f"coalesce(vp.price, {_PRODUCT_PRICE})"

# Rating „shrunk" (Bayesian) — un 5.0 cu 1 recenzie NU mai îngroapă un 4.6 cu 200 (cold-start).
# Prior C≈30 spre media 4.0: (n*rating + C*4.0)/(n + C). Pur SQL, `review_count` deja selectat.
_SHRUNK_RATING = (
    "((coalesce(p.review_count, 0) * coalesce(p.rating, 0) + 30 * 4.0)"
    " / (coalesce(p.review_count, 0) + 30))"
)

# Moduri de sortare (allowlist → zero injection; sort_mode e structural, nu param bindabil).
_VALID_SORT = frozenset({"relevance", "price_asc", "price_desc", "rating_desc"})

# NX-226 — rangul lexical aduna două semnale cu SCALE diferite. `ts_rank_cd` peste `search_tsv`
# trăiește tipic în 0,01–0,3: migrările 015/033 construiesc vectorul FĂRĂ `setweight`, deci toate
# lexemele au greutatea D (0.1). `similarity` (pg_trgm) trăiește în 0,3–1, pentru că pragul `%` e
# 0.3. Suma brută `ts_rank_cd + similarity` înseamnă deci că typo-match-ul decide practic singur
# ordinea, iar fraza naturală bine potrivită pierde în fața unui nume care „seamănă". La catalogul
# de azi abia se vede; la 5k+ produse decide CINE intră în pool-ul de candidați — exact ce fuziunea
# RRF nu mai poate repara în aval.
#
# De ce NU `ts_rank_cd(..., 32)` (rank/(rank+1)), cum propunea prima formulare a cardului: e
# monoton și mărginit, dar pentru valorile mici de aici e practic identitatea (0,1 → 0,09). Ar
# lăsa FTS-ul la fel de mic și, înmulțit cu 0.6, l-ar face și mai slab decât azi — adică exact
# opusul scopului. Mărginit ≠ comparabil.
#
# Normalizăm RELATIV la pool-ul de candidați AL ACELUIAȘI query (`max(...) over ()`): fiecare
# semnal ajunge în [0,1] raportat la cel mai bun candidat al lui, apoi ponderi explicite. Scorul
# nu are sens între query-uri diferite — și nici nu trebuie: e folosit exclusiv ca `ORDER BY` în
# interiorul unui singur query. Fereastra vede toate rândurile care trec de `WHERE`, înainte de
# `LIMIT`, deci normalizarea e peste tot pool-ul, nu peste primele 50.
#
# Ponderile sunt constante de MODUL, nu config per business: e corectitudinea formulei (ca RRF_K
# în fusion.py), nu o preferință de tenant.
_LEX_W_FTS = 0.6  # fraza naturală = semnalul primar
_LEX_W_TRGM = 0.4  # typo/SKU = plasă secundară; 0.4 ≠ 0, „sanpon" trebuie să găsească „șampon"


# 046 — treptele potrivirii lexicale, în ordinea în care se încearcă. Ordinea E contractul:
# precizie întâi, acoperire după, plasă de typo la urmă. O treaptă rulează DOAR dacă precedenta
# n-a întors nimic, deci drumul obișnuit rămâne un singur query.
#
#   strict   toți termenii de conținut (ȘI) — cererea, așa cum a fost formulată
#   relaxed  oricare dintre termeni (SAU), ordonat pe rang — „nu am tot, dar am ce contează"
#   fuzzy    word_similarity pe nume — plasa de typo, singura care costă o scanare cu funcții
#
# De ce `<%` (word_similarity) și NU `%` (similarity), cum era până acum: `%` compară interogarea
# cu numele ÎNTREG, iar numele din catalogul SOLE au 60-90 de caractere. Un typo de 6 litere are
# similaritate ~0,05 cu un nume de 80 — sub orice prag. Măsurat pe catalogul real: `%` prinde
# ZERO din typo-urile testate („sampoon", „vitmina c", „protectei solara", „hidratnata"), deci
# brațul de typo nu funcționa deloc, dar plătea 220 ms pe FIECARE căutare, fiindcă evalua
# `similarity` pe toate cele 2.758 de rânduri. `<%` compară cu cel mai bun CUVÂNT din nume și le
# prinde („sampoon" → 75, „vitmina c" → 86). Mutat pe treapta 3, costul se plătește doar când
# altfel am fi întors tăcere.
_LEXICAL_STRICT = "strict"
_LEXICAL_RELAXED = "relaxed"
_LEXICAL_FUZZY = "fuzzy"
_LEXICAL_STEPS = (_LEXICAL_STRICT, _LEXICAL_RELAXED, _LEXICAL_FUZZY)

# Pragul treptei de typo, scris EXPLICIT în cod și nu lăsat pe seama GUC-ului `pg_trgm`
# (`word_similarity_threshold`, implicit tot 0,6, pe care îl folosește operatorul `<%`): un `SET`
# făcut de altcineva pe aceeași sesiune ar muta tăcut pragul căutării. Cele două coincid azi, iar
# predicatul explicit e autoritatea; `<%` rămâne pentru că e partea INDEXABILĂ.
#
# Calibrat pe typo-uri reale, măsurat pe catalogul SOLE: la 0,6 „sampoon" ajunge la șampoane (0,70),
# „hidratnata" la produse de hidratare (0,73), „vitmina c" la produse cu vitamina C (0,62). La 0,75
# toate trei dau ZERO — adică plasa nu prinde nimic. Numărul de candidați peste prag nu e o
# problemă de precizie: `ORDER BY word_similarity DESC` + `LIMIT pool` lasă în pool doar cele mai
# apropiate. „sanpon" rămâne neprins la orice prag rezonabil (n↔m distruge prea multe trigrame) —
# o plasă de typo nu e un corector ortografic.
_WORD_SIM_MIN = 0.6


def _lexical_steps(v2: bool, terms: list[str]) -> tuple[str, ...]:
    """Treptele pe care le are rost să le încerce ACEASTĂ interogare.

    Cu kill-switch-ul stins sau fără niciun termen, rămâne clauza unică de dinainte.

    Treapta relaxată se sare la UN singur termen, fiindcă `SAU` peste un termen e ACELAȘI tsquery
    ca `ȘI` peste el: ar fi un query identic, executat a doua oară, pe drumul pe care oricum n-am
    găsit nimic. Cu un singur cuvânt, singura degradare care mai spune ceva e plasa de typo."""
    if not v2 or not terms:
        return (_LEXICAL_STRICT,)
    if len(terms) == 1:
        return (_LEXICAL_STRICT, _LEXICAL_FUZZY)
    return _LEXICAL_STEPS


def _lexical_rank_expr(q_ph: str) -> str:
    """Expresia de rang pentru `sort_mode='relevance'` în `search_products_lexical`.

    Kill-switch `lexical_rank_v2_enabled` (default OFF) → suma brută de dinainte de NX-226,
    byte-identic. Nu atinge `WHERE` (recall-ul), doar ordinea candidaților."""
    fts = f"ts_rank_cd(p.search_tsv, websearch_to_tsquery('simple', ro_unaccent({q_ph})))"
    trgm = f"similarity(ro_unaccent(p.name), ro_unaccent({q_ph}))"
    if not get_settings().lexical_rank_v2_enabled:
        return f"{fts} + {trgm}"
    # `nullif(max(...), 0)` → pool fără niciun match FTS (doar trgm) dă NULL, nu diviziune cu
    # zero; `coalesce(..., 0)` îl duce înapoi la 0, deci semnalul lipsă contribuie zero.
    return (
        f"{_LEX_W_FTS} * coalesce({fts} / nullif(max({fts}) over (), 0), 0)"
        f" + {_LEX_W_TRGM} * coalesce({trgm} / nullif(max({trgm}) over (), 0), 0)"
    )


# Varianta NU are fereastră proprie: MOȘTENEȘTE fereastra produsului (promoția e a produsului,
# nuanțele doar o poartă). Fără asta, o promoție expirată ar rămâne activă pe variante — adică fix
# pe prețul EFECTIV, cel pe care îl vede clientul.
_VARIANT_SALE_ON = f"v.sale_price is not null and v.sale_price < v.price and {_SALE_WINDOW_OK}"

# NX-118: array compact de variante (cap 12, cele mai ieftine) hidratat pe read path → validatorul
# vede prețurile per-variantă reale (50ml vs 100ml) și modelul etichetele/SKU. Neutru de vertical
# (nuanțe beauty / mărimi fashion / fitment auto — `label` vine din DB). `vp` (scalarul min) rămâne
# (îl folosesc _EFFECTIVE_PRICE + sortarea). Fragment partajat de `_SELECT`/`_DETAIL_SELECT` (DRY).
# Perf: rulează pe tot pool-ul de fuziune (ca lateralele `vp`/`img` existente), dar e un index-scan
# ieftin pe idx_variants_product(product_id), ≤16 rânduri — îl ținem și pe `_SELECT` ca validatorul
# să aibă prețurile per-variantă pe ORICE cale (search/detail), robust la dedup.
_VARIANTS_AGG = f"""
    left join lateral (
        select jsonb_agg(
            jsonb_build_object(
                'id', v.id::text,
                'variant_id', v.id::text,
                'label', v.label,
                'sku', v.sku,
                'price', (case when {_VARIANT_SALE_ON}
                               then v.sale_price else v.price end)::float8,
                'list_price',
                    (case when {_VARIANT_SALE_ON} then v.price end)::float8,
                'stock', v.stock,
                'color_hex', v.color_hex,
                'attributes', coalesce(v.attributes, '{{}}'::jsonb),
                'shade', v.attributes->>'shade',
                'undertone', v.attributes->>'undertone',
                'depth', v.attributes->>'depth',
                'net_content_value', v.net_content_value::float8,
                'net_content_unit', v.net_content_unit,
                'price_per_unit', v.price_per_unit::float8,
                'gtin', v.gtin,
                'image_url', v.image_url
            ) order by (case when {_VARIANT_SALE_ON}
                             then v.sale_price else v.price end) asc
        ) as variants
        from (
            select * from product_variants
            where product_id = p.id and business_id = p.business_id
            order by (case when {_SALE_WINDOW_OK} and sale_price is not null
                           and sale_price < price then sale_price else price end) asc
            limit 16
        ) v
    ) vr on true
"""


def _row_to_product(r: asyncpg.Record) -> dict[str, Any]:
    """`dict(r)` + decodează jsonb (NX-118). asyncpg întoarce jsonb ca STR (fără codec) →
    `json.loads`: `variants` → `list[dict]` (NULL → `[]`); `attributes` → `dict` (NULL → `{}`,
    pentru fațetele de comparație, Tier 2). Orice altă coloană intactă."""
    d = dict(r)
    if "variants" in d:
        v = d["variants"]
        if isinstance(v, str):
            try:
                d["variants"] = json.loads(v)
            except (ValueError, TypeError):
                d["variants"] = []
        elif v is None:
            d["variants"] = []
    if "attributes" in d:
        a = d["attributes"]
        if isinstance(a, str):
            try:
                d["attributes"] = json.loads(a)
            except (ValueError, TypeError):
                d["attributes"] = {}
        elif a is None:
            d["attributes"] = {}
    # NX-169: graf PDP (168e-2) — sections (json_agg → str) + badges (text[] → list). NULL → [].
    if "sections" in d:
        s = d["sections"]
        if isinstance(s, str):
            try:
                d["sections"] = json.loads(s)
            except (ValueError, TypeError):
                d["sections"] = []
        elif s is None:
            d["sections"] = []
    if "badges" in d and d["badges"] is None:
        d["badges"] = []
    if "ingredients_db" in d and d["ingredients_db"] is None:
        d["ingredients_db"] = []
    if "faqs" in d:
        fq = d["faqs"]
        if isinstance(fq, str):
            try:
                d["faqs"] = json.loads(fq)
            except (ValueError, TypeError):
                d["faqs"] = []
        elif fq is None:
            d["faqs"] = []
    if "reviews_list" in d:
        rv = d["reviews_list"]
        if isinstance(rv, str):
            try:
                d["reviews_list"] = json.loads(rv)
            except (ValueError, TypeError):
                d["reviews_list"] = []
        elif rv is None:
            d["reviews_list"] = []
    return d


def _order_clause(sort_mode: str, *, qvec_ph: str | None = None) -> str:
    """`ORDER BY` pe mod de sortare + tie-break determinist `p.id` (omoară ordonarea instabilă pe
    egalități → cache + golden stabile). Filter-then-sort: constrângerile dure stau în WHERE, AICI
    doar sortăm. Kill-switch `SEARCH_SORT_MODE_ENABLED=False` → `ORDER BY`-ul vechi (byte-identic).
    Pe calea semantică (`qvec_ph`): `relevance` = cosine; price/rating = sort explicit pe subsetul
    deja filtrat semantic (NB: sub HNSW = cel-mai-ieftin-din-recall, nu global — vezi ARCH §P3)."""
    if not get_settings().search_sort_mode_enabled:
        # Kill-switch: revert EXACT — pe semantic = cosine (qvec_ph), pe SQL = rating desc.
        if qvec_ph is not None:
            return f" order by pe.embedding <=> {qvec_ph}::vector"
        return f" order by p.rating desc, {_EFFECTIVE_PRICE} asc"
    mode = sort_mode if sort_mode in _VALID_SORT else "relevance"
    if mode == "price_asc":
        return f" order by {_EFFECTIVE_PRICE} asc, {_SHRUNK_RATING} desc, p.id"
    if mode == "price_desc":
        return f" order by {_EFFECTIVE_PRICE} desc, {_SHRUNK_RATING} desc, p.id"
    if mode == "rating_desc":
        return f" order by {_SHRUNK_RATING} desc, {_EFFECTIVE_PRICE} asc, p.id"
    # relevance
    if qvec_ph is not None:
        return f" order by pe.embedding <=> {qvec_ph}::vector, p.id"
    return f" order by {_SHRUNK_RATING} desc, {_EFFECTIVE_PRICE} asc, p.id"


def _content_status_pred(business_id_ph: str = "$1") -> str | None:
    """NX-171c: predicat quality-gate pentru read-path (discovery). Întoarce `None` (fără filtru)
    când kill-switch-ul GLOBAL e OFF. Altfel, filtru PER-TENANT: arată doar 'published' DACĂ
    tenantul a optat (`businesses.settings->>'content_status_filter'`), altfel catalog integral
    (fără outage). Sub-query-ul scalar e NECORELAT (constant $1) → evaluat O DATĂ ca initplan, zero
    cost per-rând. NULL/absent setting → `false` → filtru inactiv. `business_id_ph` = placeholder-ul
    lui business_id în query-ul apelant (mereu `$1` în funcțiile de catalog)."""
    if not get_settings().content_status_filter_enabled:
        return None
    return (
        "(p.content_status = 'published' or coalesce("
        "(select (settings->>'content_status_filter')::boolean from businesses "
        f"where id = {business_id_ph}), false) = false)"
    )


# Câmpuri per produs (CLAUDE.md): id, name, brand, price, url, ai_summary, stock,
# availability + image (prima poză, pentru cardurile de produs — W1).
_SELECT = f"""
    select
        p.id::text                  as id,
        p.name                      as name,
        b.name                      as brand,
        {_EFFECTIVE_PRICE}::float8  as price,
        p.product_url               as url,
        p.ai_summary                as ai_summary,
        p.stock_total               as stock,
        p.availability              as availability,
        img.url                     as image,
        p.rating::float8            as rating,
        p.review_count              as review_count,
        prs.top_pros[1]             as review_pro,
        prs.top_pros                as top_pros,
        {_SALE_ACTIVE} as on_sale,
        -- IZI-anchor: preț ORIGINAL (tăiat), DOAR la reducere reală; altfel NULL → cardul nu
        -- afișează „de la X" fals pe o variantă mai mică. `price` rămâne efectivul curent.
        (case when {_SALE_ACTIVE} then p.price end)::float8
                                    as list_price,
        p.attributes->'concerns'    as concerns,
        p.attributes                as attributes,
        -- NX-240: moneda + momentul VERIFICĂRII. `currency` fiindcă o sumă fără unitate nu e o
        -- sumă (grounding-ul o marchează UNKNOWN); `synced_at` fiindcă `updated_at` spune doar
        -- când s-a atins rândul, nu când s-a confruntat cu sursa — iar CTA-urile de comerț se
        -- sprijină pe verificare, nu pe atingere.
        p.currency                  as currency,
        p.synced_at                 as synced_at,
        vr.variants                 as variants
    from products p
    left join brands b on b.id = p.brand_id
    left join categories c on c.id = p.primary_category_id
    -- P7: `business_id` EXPLICIT și pe join, nu doar pe tabela condusă. `product_review_summaries`
    -- are cheia primară pe `product_id` singur, deci join-ul „mergea" fără el — dar izolarea nu
    -- trebuie să depindă de forma unei chei primare care se poate schimba.
    left join product_review_summaries prs
           on prs.product_id = p.id and prs.business_id = p.business_id
    left join lateral (
        select min(case when {_SALE_WINDOW_OK} and v.sale_price is not null
                         and v.sale_price < v.price then v.sale_price else v.price end) as price
        from product_variants v
        where v.product_id = p.id and v.business_id = p.business_id
    ) vp on true
    left join lateral (
        select pi.url from product_images pi
        where pi.product_id = p.id
        order by pi.position asc nulls last
        limit 1
    ) img on true
{_VARIANTS_AGG}
"""


def _feature_clause(facet_keys: tuple[str, ...], values: list[str], placeholder: Any) -> str:
    """Tier 2b p2: condiție SQL „produsul ARE una din valorile cerute", în UNIUNEA atributelor-array
    din `facet_keys` (ex. key_ingredients), cu match NORMALIZAT (lower + strip diacritice RO, ca
    `normalize`) → „niacinamida"/„niacinamidă" se potrivesc. Chei PARAMETRIZATE (safe). `values`
    deja normalizate de caller."""
    arrays = []
    for k in facet_keys:
        kp = placeholder(k)
        arrays.append(
            f"(case when jsonb_typeof(p.attributes->{kp})='array' "
            f"then p.attributes->{kp} else '[]'::jsonb end)"
        )
    union = " || ".join(arrays)
    return (
        f"exists (select 1 from jsonb_array_elements_text({union}) fe "
        f"where translate(lower(fe), 'ăâîșț', 'aaist') = any({placeholder(values)}::text[]))"
    )


def _facet_filter_clause(filters: Mapping[str, Sequence[str]], placeholder: Any) -> str:
    """Filtru GENERIC pe fațete, pentru dimensiuni DESCOPERITE din catalog — nu pentru o listă de
    chei știute de cod.

    Contează pentru că altfel fiecare vertical nou ar cere o coloană nouă în semnătură: `concerns`
    pentru cosmetice, `agent_frigorific` pentru HVAC, `compatibil_cu` pentru auto. Aici cheia e un
    parametru ca oricare altul, deci un tenant care are în `attributes` ceva ce noi n-am văzut
    niciodată se filtrează corect fără nicio linie de cod.

    Dimensiunile se leagă cu AND (nevoi diferite se cumulează), valorile aceleiași dimensiuni cu OR
    (sunt alternative). Se acceptă atât fațete-listă cât și fațete-scalar, fiindcă descoperirea le
    admite pe amândouă. Cheia e PARAMETRIZATĂ (`attributes->$n`), niciodată interpolată în SQL —
    aceeași regulă ca `_feature_clause`.

    Valorile vin EXCLUSIV din rezoluția contra vocabularului (`src/catalog/vocabulary.py`), deci
    fiecare are produse în spate prin construcție: filtrul poate îngusta, dar nu poate goli din
    cauza unui cuvânt inexistent."""
    parts: list[str] = []
    for key in sorted(filters):
        values = [v for v in filters[key] if v]
        if not values:
            continue
        kp = placeholder(key)
        vp = placeholder(list(values))
        parts.append(
            f"(case when jsonb_typeof(p.attributes->{kp}) = 'array' "
            f"then (p.attributes->{kp}) ?| {vp}::text[] "
            f"else (p.attributes->>{kp}) = any({vp}::text[]) end)"
        )
    return " and ".join(parts)


def _variant_label_clause(label: str, placeholder: Any) -> str:
    """NX-135: produsul are o VARIANTĂ cu eticheta cerută (nuanță/mărime) — filtru DUR pentru
    fallback-ul gradat („alte game care CHIAR au Warm Beige"). Match NORMALIZAT (lower + strip
    diacritice RO, ca `_feature_clause`/`normalize`) + substring → „warm beige" prinde „Warm Beige".
    Corelat pe produsul din SELECT (`v.product_id = p.id`); scope moștenit din `business_id`."""
    lp = placeholder(label)
    return (
        "exists (select 1 from product_variants v where v.product_id = p.id "
        f"and translate(lower(v.label), 'ăâîșț', 'aaist') "
        f"like '%' || translate(lower({lp}), 'ăâîșț', 'aaist') || '%')"
    )


def _category_clause(category: str | Sequence[str], placeholder: Any) -> str:
    """NX-167 (A): predicatul de categorie, ca UN SINGUR `exists(...)` INLINE (se adaugă la `conds`
    ca orice altă condiție — fără CTE, fără restructurarea query-urilor).

    Cu `SEARCH_CATEGORY_TREE_ENABLED`: produsul e „în categorie" dacă ORICARE din categoriile lui
    — `primary_category_id` SAU orice `product_category_map` — e categoria CERUTĂ sau un DESCENDENT
    al ei (materialized path `categories.path`). Repară „cerere pe părinte (machiaj) ratează copiii
    (fond-de-ten)". `reqc`/`sub`/`m` NU se leagă de aliasurile din SELECT (`c`/`p`) → fără
    coliziune; corelat pe `p.business_id`/`p.primary_category_id`/`p.id` (scope P7).

    Fără flag (OFF): match exact pe slug/nume al `primary_category_id`. `category` = slug SAU nume.

    Acceptă și o LISTĂ de categorii, ca „oricare dintre". E nevoie pentru rezoluțiile ambigue
    (`src/catalog/vocabulary.py`): «cremă» nu identifică o familie anume, dar rămâne o cerere de
    cremă — constrângerea pe uniunea familiilor de creme păstrează sensul cererii și ține măștile
    afară, în timp ce renunțarea la constrângere le-ar lăsa să câștige pe text.

    O listă GOALĂ e eroare de programare, nu „fără filtru": un predicat care nu se potrivește cu
    nimic ar readuce exact zero-ul tăcut pe care rezoluția îl elimină. Apelantul care n-are chei
    nu trebuie să cheme funcția."""
    keys = [category] if isinstance(category, str) else list(category)
    lowered = [k.lower() for k in keys if k and k.strip()]
    if not lowered:
        raise ValueError(
            "_category_clause fără nicio categorie: un filtru gol ar goli rezultatul tăcut. "
            "Apelantul trebuie să sară peste predicat când rezoluția n-a produs chei."
        )
    if not get_settings().search_category_tree_enabled:
        ph = placeholder(lowered)
        return f"(lower(c.slug) = any({ph}::text[]) or lower(c.name) = any({ph}::text[]))"
    cat_ph = placeholder(lowered)  # un singur placeholder, reutilizat de 2 ori (aceeași valoare)
    return (
        "exists (select 1 from categories reqc "
        "join categories sub on sub.business_id = reqc.business_id "
        "and (sub.id = reqc.id or sub.path like reqc.path || '/%') "
        "where reqc.business_id = p.business_id "
        f"and (lower(reqc.slug) = any({cat_ph}::text[]) "
        f"or lower(reqc.name) = any({cat_ph}::text[])) "
        "and (sub.id = p.primary_category_id or exists (select 1 from product_category_map m "
        "where m.product_id = p.id and m.category_id = sub.id)))"
    )


async def search_products(
    conn: asyncpg.Connection,
    business_id: str,
    *,
    category: str | Sequence[str] | None = None,
    brand: str | None = None,
    concerns: list[str] | None = None,
    facet_filters: Mapping[str, Sequence[str]] | None = None,
    features: list[str] | None = None,
    searchable_facets: tuple[str, ...] = (),
    price_max: float | None = None,
    query_text: str | None = None,
    sort_mode: str = "relevance",
    in_stock_only: bool = False,
    limit: int = 6,
) -> list[dict[str, Any]]:
    """Caută produse active pentru un tenant, cu filtre SQL dure + mod de sortare explicit.

    Toate filtrele sunt opționale și se combină cu AND. Returnează max `limit`
    produse (hard cap 6 — principiul „max 6 produse" din arhitectură), fiecare
    cu cele 8 câmpuri. `conn` trebuie să fie deja tenant-scoped (tenant_conn).

    `sort_mode` (filter-then-sort): `price_asc` pt preț („cel mai ieftin"), `rating_desc`
    pt „cel mai bun", altfel `relevance`. `in_stock_only` = filtru DUR pe disponibilitate (doar
    cerut explicit). `concerns` filtrează pe `attributes->'concerns'` (`?|` = oricare).
    """
    limit = min(limit, 6)

    conds = ["p.business_id = $1", "p.status = 'active'"]
    params: list[Any] = [business_id]

    def placeholder(value: Any) -> str:
        params.append(value)
        return f"${len(params)}"

    if category:
        # NX-167 (A): match pe arbore (primary + product_category_map + descendenți) sau, cu flag
        # OFF, exact pe slug/nume al primary_category_id (byte-identic cu vechiul cod).
        conds.append(_category_clause(category, placeholder))
    if brand:
        # NX-178: și brandul se caută fără diacritice („petala" → „Petala", „loreal" → „L'Oréal")
        conds.append(f"ro_unaccent(b.name) like ro_unaccent({placeholder(f'%{brand}%')})")
    if concerns:
        conds.append(f"(p.attributes->'concerns') ?| {placeholder(concerns)}::text[]")
    if facet_filters and (fc := _facet_filter_clause(facet_filters, placeholder)):
        conds.append(fc)
    if features and searchable_facets:
        conds.append(_feature_clause(searchable_facets, features, placeholder))
    if price_max is not None:
        conds.append(f"{_EFFECTIVE_PRICE} <= {placeholder(price_max)}")
    if in_stock_only:
        conds.append("p.availability in ('in_stock', 'low_stock')")
    if query_text:
        conds.append(f"p.name ilike {placeholder(f'%{query_text}%')}")
    if cs := _content_status_pred():  # NX-171c: doar 'published' (per-tenant, gated)
        conds.append(cs)

    sql = (
        _SELECT
        + " where "
        + " and ".join(conds)
        + _order_clause(sort_mode)
        + f" limit {placeholder(limit)}"
    )

    rows = await conn.fetch(sql, *params)
    return [_row_to_product(r) for r in rows]


async def search_products_lexical(
    conn: asyncpg.Connection,
    business_id: str,
    query_text: str,
    *,
    category: str | Sequence[str] | None = None,
    brand: str | None = None,
    concerns: list[str] | None = None,
    facet_filters: Mapping[str, Sequence[str]] | None = None,
    features: list[str] | None = None,
    searchable_facets: tuple[str, ...] = (),
    variant_label: str | None = None,
    price_max: float | None = None,
    sort_mode: str = "relevance",
    in_stock_only: bool = False,
    locale: str | None = None,
    pool: int = 50,
) -> list[dict[str, Any]]:
    """Lexical REAL (NX-113a) — înlocuiește `p.name ILIKE '%q%'`. ACELEAȘI filtre dure ca
    `search_products` (paritate). Întoarce ~`pool` rânduri; pe `relevance` POZIȚIA în listă =
    rangul pentru RRF (NX-113b). Pe sort explicit (price/rating) delegă `_order_clause`. `conn`
    tenant-scoped (P7: `business_id = $1`).

    046 — potrivirea nu mai e o singură condiție, ci o SCARĂ de trei trepte (vezi `_LEXICAL_STEPS`),
    fiindcă varianta de dinainte întorcea ZERO pe majoritatea frazelor reale de client.

    Cauza, măsurată pe catalogul SOLE: `websearch_to_tsquery` leagă toate cuvintele cu ȘI, iar
    configurația `'simple'` nu elimină niciun cuvânt gol. „sampon pentru par gras" cerea ca
    produsul să conțină literalmente și „pentru". Din 18 fraze scrise ca de client, **13 întorceau
    zero rezultate** — nu rezultate slabe, ci tăcere, adică exact ce interzice P6. Cu termenii de
    conținut extrași în cod (`src/catalog/query_terms.py`) și descrierea în index (046), aceleași
    18 fraze întorc **0 zerouri**, cu latența medie la jumătate (289 ms → 136 ms): treapta strictă
    le servește pe aproape toate, iar funcția scumpă de trigrame a ieșit de pe drumul obișnuit.

    Kill-switch `lexical_query_v2_enabled` OFF → clauza unică de dinainte (FTS SAU `similarity` pe
    nume), byte-identic. `locale` alege lista de cuvinte goale; `None` = nicio eliminare (P11:
    limba e cheie, nu constantă).
    """
    v2 = get_settings().lexical_query_v2_enabled
    terms = content_terms(query_text, locale) if v2 else []
    steps = _lexical_steps(v2, terms)
    for step in steps:
        rows = await _lexical_fetch(
            conn,
            business_id,
            query_text=query_text,
            terms=terms,
            step=step,
            v2=v2,
            category=category,
            brand=brand,
            concerns=concerns,
            facet_filters=facet_filters,
            features=features,
            searchable_facets=searchable_facets,
            variant_label=variant_label,
            price_max=price_max,
            sort_mode=sort_mode,
            in_stock_only=in_stock_only,
            pool=pool,
        )
        if rows:
            # Degradarea trebuie să fie VIZIBILĂ. Un rezultat obținut prin relaxare sau prin plasa
            # de typo nu e același lucru cu unul care a potrivit cererea așa cum a fost formulată:
            # măsurat pe catalogul SOLE, singura interogare pe care relaxarea a servit-o din 18 a
            # fost „parfum de dama" — un raft pe care SOLE nu îl are, iar rezultatul a fost un
            # spray parfumat de păr. Marcăm treapta pe fiecare produs (ca `variant_match` la
            # NX-135), ca apelantul să poată număra, dezvălui sau respinge. Fără marcaj, un „n-am
            # asta, dar am ceva vag înrudit" ar arăta identic cu o potrivire bună.
            if step != _LEXICAL_STRICT:
                for r in rows:
                    r["lexical_step"] = step
            return rows
    return []


async def _lexical_fetch(
    conn: asyncpg.Connection,
    business_id: str,
    *,
    query_text: str,
    terms: list[str],
    step: str,
    v2: bool,
    category: str | Sequence[str] | None,
    brand: str | None,
    concerns: list[str] | None,
    facet_filters: Mapping[str, Sequence[str]] | None,
    features: list[str] | None,
    searchable_facets: tuple[str, ...],
    variant_label: str | None,
    price_max: float | None,
    sort_mode: str,
    in_stock_only: bool,
    pool: int,
) -> list[dict[str, Any]]:
    """O treaptă a scării lexicale. Filtrele dure sunt IDENTICE pe toate treptele — se relaxează
    potrivirea de TEXT, niciodată constrângerile (preț, brand, categorie, variantă, stoc). Scara
    din `search_products_tool` face lucrul complementar: relaxează filtrele, cu textul fix."""
    conds = ["p.business_id = $1", "p.status = 'active'"]
    params: list[Any] = [business_id]

    def placeholder(value: Any) -> str:
        params.append(value)
        return f"${len(params)}"

    rank_expr: str
    if not v2:
        q_ph = placeholder(query_text)  # un singur placeholder, reutilizat în match + rank
        # Comportamentul de dinainte de 046, păstrat sub kill-switch. NX-178: AMBELE capete trec
        # prin `ro_unaccent` (033) — `search_tsv` e construit peste text normalizat, deci aici se
        # normalizează doar interogarea.
        conds.append(
            f"(p.search_tsv @@ websearch_to_tsquery('simple', ro_unaccent({q_ph}))"
            f" or ro_unaccent(p.name) % ro_unaccent({q_ph}))"
        )
        rank_expr = _lexical_rank_expr(q_ph)
    elif step == _LEXICAL_FUZZY:
        # Plasa de typo. `<%` e partea INDEXABILĂ (`idx_products_name_ro_trgm`), iar predicatul
        # explicit e cel care STABILEȘTE pragul — vezi `_WORD_SIM_MIN` pentru de ce nu-l lăsăm pe
        # seama GUC-ului.
        q_ph = placeholder(fold(query_text))
        conds.append(
            f"(ro_unaccent({q_ph}) <% ro_unaccent(p.name)"
            f" and word_similarity(ro_unaccent({q_ph}), ro_unaccent(p.name)) >= {_WORD_SIM_MIN})"
        )
        rank_expr = f"word_similarity(ro_unaccent({q_ph}), ro_unaccent(p.name))"
    else:
        # Treptele de text: aceeași expresie de rang, tsquery diferit. `ts_rank_cd` are sens abia
        # după 046, care pune ponderi pe câmpuri (nume A / ai_summary B / descriere C) — înainte
        # tot vectorul era pe greutatea D și rangul era practic plat.
        phrase = strict_query(terms) if step == _LEXICAL_STRICT else relaxed_query(terms)
        q_ph = placeholder(phrase)
        tsq = f"websearch_to_tsquery('simple', ro_unaccent({q_ph}))"
        conds.append(f"p.search_tsv @@ {tsq}")
        rank_expr = f"ts_rank_cd(p.search_tsv, {tsq})"

    if category:
        conds.append(_category_clause(category, placeholder))  # NX-167 (A): match pe arbore
    if brand:
        # NX-178: și brandul se caută fără diacritice („petala" → „Petala", „loreal" → „L'Oréal")
        conds.append(f"ro_unaccent(b.name) like ro_unaccent({placeholder(f'%{brand}%')})")
    if concerns:
        conds.append(f"(p.attributes->'concerns') ?| {placeholder(concerns)}::text[]")
    if facet_filters and (fc := _facet_filter_clause(facet_filters, placeholder)):
        conds.append(fc)
    if features and searchable_facets:
        conds.append(_feature_clause(searchable_facets, features, placeholder))
    if variant_label:  # NX-135: filtru DUR pe eticheta de variantă (fallback gradat)
        conds.append(_variant_label_clause(variant_label, placeholder))
    if price_max is not None:
        conds.append(f"{_EFFECTIVE_PRICE} <= {placeholder(price_max)}")
    if in_stock_only:
        conds.append("p.availability in ('in_stock', 'low_stock')")
    if cs := _content_status_pred():  # NX-171c: doar 'published' (per-tenant, gated)
        conds.append(cs)

    if sort_mode == "relevance":
        order = f" order by ({rank_expr}) desc, p.id"
    else:
        order = _order_clause(sort_mode)  # price/rating explicit → sort pe subsetul lexical filtrat

    sql = _SELECT + " where " + " and ".join(conds) + order + f" limit {placeholder(pool)}"
    rows = await conn.fetch(sql, *params)
    return [_row_to_product(r) for r in rows]


def semantic_embedding_doc_type(explicit: str | None = None) -> str:
    """Selectează documentul semantic activ; override-ul e pentru benchmark/shadow intern."""
    if explicit is not None:
        return explicit
    return "search_document_v1" if get_settings().search_shadow_enabled else "product"


async def has_embeddings(
    conn: asyncpg.Connection, business_id: str, *, embedding_doc_type: str | None = None
) -> bool:
    """True dacă tenantul are măcar un `product_embedding` PENTRU doc_type/model-ul ACTIV.

    Decide calea din `search_products_tool`: semantic (JOIN pe product_embeddings) doar dacă există
    embeddings pe care read-path-ul le va găsi efectiv. NX-171d: read-path-ul filtrează
    `doc_type='product'` + modelul activ, deci și acest check TREBUIE să le filtreze — altfel
    embeddings de alt tip/model vechi ar declanșa calea semantică inutil (JOIN care nu întoarce
    nimic). Un singur SELECT scoped (P7); ieftin (embeddings apar după job, nu în tur)."""
    row = await conn.fetchrow(
        "select 1 from product_embeddings "
        "where business_id = $1 and doc_type = $2 and model = $3 limit 1",
        business_id,
        semantic_embedding_doc_type(embedding_doc_type),
        get_settings().model_embed,
    )
    return row is not None


# Detalii bogate per produs (tool-uri G7): câmpurile de bază + rezumatul de recenzii (D3).
_DETAIL_SELECT = f"""
    select
        p.id::text                  as id,
        p.name                      as name,
        b.name                      as brand,
        {_EFFECTIVE_PRICE}::float8  as price,
        p.product_url               as url,
        p.ai_summary                as ai_summary,
        p.stock_total               as stock,
        p.availability              as availability,
        img.url                     as image,
        p.rating::float8            as rating,
        p.review_count              as review_count,
        p.attributes                as attributes,
        p.currency                  as currency,
        p.synced_at                 as synced_at,
        -- IZI-anchor: preț original (tăiat) DOAR la reducere reală (vezi _SELECT); altfel NULL.
        (case when {_SALE_ACTIVE} then p.price end)::float8
                                    as list_price,
        prs.summary                 as review_summary,
        prs.top_pros                as top_pros,
        prs.top_cons                as top_cons,
        prs.sentiment::float8       as sentiment,
        sec.sections                as sections,
        bdg.badges                  as badges,
        ing.names                   as ingredients_db,
        rvw.items                   as reviews_list,
        faq.items                   as faqs,
        -- NX-191: faptele de livrare (clasa + data de revenire). Promisiunea CONCRETĂ se
        -- calculează în cod (src/commerce/delivery), nu în SQL: depinde de ceas și de config.
        p.delivery_class            as delivery_class,
        p.restock_date              as restock_date,
        vr.variants                 as variants
    from products p
    left join brands b on b.id = p.brand_id
    -- P7: `business_id` EXPLICIT și pe join, nu doar pe tabela condusă. `product_review_summaries`
    -- are cheia primară pe `product_id` singur, deci join-ul „mergea" fără el — dar izolarea nu
    -- trebuie să depindă de forma unei chei primare care se poate schimba.
    left join product_review_summaries prs
           on prs.product_id = p.id and prs.business_id = p.business_id
    left join lateral (
        select min(case when {_SALE_WINDOW_OK} and v.sale_price is not null
                         and v.sale_price < v.price then v.sale_price else v.price end) as price
        from product_variants v
        where v.product_id = p.id and v.business_id = p.business_id
    ) vp on true
    left join lateral (
        select pi.url from product_images pi
        where pi.product_id = p.id
        order by pi.position asc nulls last
        limit 1
    ) img on true
    -- NX-168e-2 graf PDP, consumat de _detail_view (NX-169): secțiuni + badge-uri de trust.
    left join lateral (
        select json_agg(
                   json_build_object('kind', s.kind, 'title', s.title, 'body', s.body)
                   order by s.position
               ) as sections
        from product_sections s where s.product_id = p.id
    ) sec on true
    left join lateral (
        select array_agg(pb.label order by pb.label) as badges
        from product_badges pb where pb.product_id = p.id
    ) bdg on true
    -- NX-169: consumă tabelul NORMALIZAT de ingrediente (168e-2) — INCI cheie din product_ingr.
    left join lateral (
        select array_agg(i.name order by pi.position) as names
        from product_ingredients pi
        join ingredients i on i.id = pi.ingredient_id
        where pi.product_id = p.id and pi.is_key
    ) ing on true
    -- NX-194: FAQ per produs (6), citit la DETALIU (nu intră în căutare — vezi migrarea 032).
    -- Tenant pe business_id (FK compus), limbă explicită (P11).
    left join lateral (
        select json_agg(
                   json_build_object('question', f.question, 'answer', f.answer)
                   order by f.position
               ) as items
        from (
            select question, answer, position from product_faqs
            where business_id = p.business_id and product_id = p.id and locale = 'ro'
            order by position limit 6
        ) f
    ) faq on true
    -- NX-169: consumă recenziile INDIVIDUALE (168e-2) — top 2 după rating, corelate pe tenant.
    left join lateral (
        select json_agg(
                   json_build_object('author', r.author, 'rating', r.rating, 'body', r.body)
                   order by r.rating desc
               ) as items
        from (
            select author, rating, body from reviews
            where product_id = p.id and business_id = p.business_id
            order by rating desc limit 2
        ) r
    ) rvw on true
{_VARIANTS_AGG}
"""


async def get_products_by_ids(
    conn: asyncpg.Connection,
    business_id: str,
    product_ids: list[str],
    *,
    limit: int = 6,
    respect_content_status: bool = False,
) -> list[dict[str, Any]]:
    """Produse active după id (tool-uri get_product_details / compare_products), cu detalii
    bogate (rating + rezumat recenzii D3). `business_id = $1` (izolare; RLS plasa). Max
    `limit` (hard cap 6). Ordinea ÎN care s-au cerut id-urile e PĂSTRATĂ (`array_position`) —
    deixis-ul ordinal („a doua"/„compară primele două") rezolvă produsul corect.

    `respect_content_status` (NX-171c): DEFAULT off — re-hidratarea produselor DEJA afișate
    (validator de preț, deixis, compare) NU trebuie filtrată (un produs arătat, devenit draft, tot
    are nevoie de preț validat). Calea care SERVEȘTE produse NOI nevăzute (`continue_search_session`
    — „mai arată-mi") trece `True` → aplică filtrul published (per-tenant), ca discovery."""
    if not product_ids:
        return []
    limit = min(limit, 6)
    cs = _content_status_pred() if respect_content_status else None
    rows = await conn.fetch(
        _DETAIL_SELECT
        + " where p.business_id = $1 and p.status = 'active' and p.id = any($2::uuid[])"
        + (f" and {cs}" if cs else "")
        + " order by array_position($2::uuid[], p.id)"
        + " limit $3",
        business_id,
        product_ids[:limit],
        limit,
    )
    return [_row_to_product(r) for r in rows]


#: Plafonul bazinului de retrieval. NU e plafonul de context al agentului (acela e 6, în
#: `get_products_by_ids`, și apără promptul). Un bazin de ranking are nevoie de mai mulți candidați
#: decât intră în răspuns, altfel „rerank" înseamnă reordonarea celor șase deja aleși.
MAX_POOL_HYDRATION = 100


async def get_products_pool_by_ids(
    conn: asyncpg.Connection,
    business_id: str,
    product_ids: list[str],
    *,
    pool: int = 30,
    respect_content_status: bool = True,
) -> list[dict[str, Any]]:
    """Hidratează un BAZIN de candidați într-un SINGUR round trip, în ordinea cerută.

    Există fiindcă `get_products_by_ids` are un cap DUR de 6 (bugetul de context al agentului):
    hidratarea unui bazin de 30 prin el înseamnă 5 interogări secvențiale pe aceeași conexiune —
    exact N+1-ul pe care NX-231 îl interzice pe drumul de tur. Aceeași izolare (`business_id = $1`),
    același quality-gate (`published`), aceeași păstrare a ordinii; se schimbă doar plafonul, și
    numai pentru calea de ranking, care nu trimite bazinul spre model.

    `respect_content_status` e DEFAULT `True` aici (invers față de helperul de re-hidratare): un
    bazin servește produse NEVĂZUTE, deci e discovery — iar discovery-ul filtrează pe `published`.
    """
    if not product_ids:
        return []
    pool = min(max(pool, 1), MAX_POOL_HYDRATION)
    cs = _content_status_pred() if respect_content_status else None
    rows = await conn.fetch(
        _DETAIL_SELECT
        + " where p.business_id = $1 and p.status = 'active' and p.id = any($2::uuid[])"
        + (f" and {cs}" if cs else "")
        + " order by array_position($2::uuid[], p.id)"
        + " limit $3",
        business_id,
        product_ids[:pool],
        pool,
    )
    return [_row_to_product(r) for r in rows]


async def get_substitutes(
    conn: asyncpg.Connection,
    business_id: str,
    product_id: str,
    *,
    limit: int = 2,
) -> list[dict[str, Any]]:
    """NX-195: alternativele PE STOC pentru un produs epuizat, din `product_relations`
    (kind='substitute', NX-171b).

    Cele 222 de relații de substitut existau de la 171b și NU erau citite de nimeni — „nu mai
    avem" era răspunsul final, deși alternativa era în DB. Filtrăm explicit ce nu ajută:
    produsul-ancoră, produsele inactive/nepublicate și cele care sunt și ele epuizate (un
    substitut epuizat nu e un substitut).

    `business_id = $1` pe AMBELE capete (P7; FK-ul compus din 027 face cross-tenant imposibil
    structural, dar predicatul rămâne mecanismul primar)."""
    cs = _content_status_pred()
    rows = await conn.fetch(
        _DETAIL_SELECT
        + " join product_relations r on r.related_id = p.id and r.business_id = p.business_id"
        + " where p.business_id = $1 and r.product_id = $2::uuid and r.kind = 'substitute'"
        + " and p.status = 'active' and p.availability <> 'out_of_stock'"
        + (f" and {cs}" if cs else "")
        + " order by r.position asc, p.id"
        + " limit $3",
        business_id,
        product_id,
        min(limit, 3),
    )
    return [_row_to_product(r) for r in rows]


async def product_category_roots(
    conn: asyncpg.Connection, business_id: str, product_ids: list[str]
) -> dict[str, str]:
    """NX-167 (C): root-branch-ul (primul segment din `categories.path`) al categoriei PRIMARE a
    fiecărui produs — pentru garda de coerență la compare (`machiaj` vs. `par` = incoerent).
    `business_id = $1` (izolare P7; RLS plasă). Produsele fără categorie/`path` sunt ABSENTE din
    dict → caller-ul e fail-open (nu blochează pe date lipsă)."""
    if not product_ids:
        return {}
    rows = await conn.fetch(
        "select p.id::text as id, split_part(c.path, '/', 1) as root "
        "from products p join categories c on c.id = p.primary_category_id "
        "where p.business_id = $1 and p.id = any($2::uuid[]) "
        "and c.path is not null and c.path <> ''",
        business_id,
        product_ids,
    )
    return {r["id"]: r["root"] for r in rows if r["root"]}


async def search_cheaper_than(
    conn: asyncpg.Connection,
    business_id: str,
    reference_ids: list[str],
    max_price_exclusive: float,
    *,
    limit: int = 6,
) -> list[dict[str, Any]]:
    """Produse active STRICT mai ieftine decât `max_price_exclusive`, în ACEEAȘI categorie ca
    produsele de referință (cele afișate), sortate pe preț crescător (P1 ARCH-product-retrieval).

    Pentru follow-up-ul „mai ieftin": ancorat pe categoria setului afișat (subquery pe id-urile lor)
    → nu aduce „cel mai ieftin gunoi" din alt raft. DOAR produse CUMPĂRABILE (în stoc) — un „cel
    mai ieftin" fără stoc e inutil. Determinist (cel mai ieftin real = rândul 1), FĂRĂ padding —
    întoarce DOAR ce e mai ieftin (1 dacă e 1). Gol = nu există nimic mai ieftin (în stoc).
    `business_id = $1` (izolare; RLS plasă). Hard cap 6."""
    if not reference_ids:
        return []
    limit = min(limit, 6)
    cs = _content_status_pred()  # NX-171c: doar 'published' (per-tenant, gated)
    sql = (
        _SELECT
        + " where p.business_id = $1 and p.status = 'active'"
        + " and p.availability in ('in_stock', 'low_stock')"
        + " and p.primary_category_id in ("
        + "   select primary_category_id from products"
        + "   where business_id = $1 and id = any($2::uuid[]) and primary_category_id is not null)"
        + " and p.id <> all($2::uuid[])"  # exclude produsele AFIȘATE: un produs în reducere nu e
        + f" and {_EFFECTIVE_PRICE} < $3"  # „mai ieftin decât el însuși" → altfel bucla pe același
        + (f" and {cs}" if cs else "")
        + f" order by {_EFFECTIVE_PRICE} asc, {_SHRUNK_RATING} desc, p.id"
        + " limit $4"
    )
    rows = await conn.fetch(sql, business_id, reference_ids, max_price_exclusive, limit)
    return [_row_to_product(r) for r in rows]


async def get_complementary_products(
    conn: asyncpg.Connection,
    business_id: str,
    anchor_id: str,
    *,
    exclude_ids: list[str] | None = None,
    limit: int = 4,
) -> list[dict[str, Any]]:
    """Produse COMPLEMENTARE produsului `anchor_id` (cross-sell „merge bine cu" / rutină, #7b).

    NX-171b: **relations-first** — citește relații EXPLICITE curate din `product_relations`
    (`complement`/`routine_next`/`accessory`), nu heuristica same-brand/concern. Doar CUMPĂRABILE
    (în stoc), excluzând ancora + coșul (`exclude_ids`), ordonate pe tip (rutina întâi) + poziția
    curată. Când ancora n-are NICIO relație (sau kill-switch `relations_first_enabled` OFF) → cade
    pe heuristica veche (`_complementary_heuristic`, byte-identic). Gol = niciun semnal (flux ok).
    `business_id = $1` (izolare; RLS plasă). Hard cap 6."""
    limit = min(limit, 6)
    exclude = list(dict.fromkeys([anchor_id, *(exclude_ids or [])]))
    if get_settings().relations_first_enabled:
        # Contract: heuristica e fallback DOAR când ancora n-are NICIO relație curată (complement/
        # rutină/accesoriu) — NU când relațiile există dar sunt neeligibile (draft/out-of-stock/în
        # coș). Altfel un produs cu rutină definită ar aluneca înapoi în heuristica same-brand când
        # pașii lui sunt temporar fără stoc. Verificăm EXISTENȚA relației separat de eligibilitate.
        if await _has_complementary_relations(conn, business_id, anchor_id):
            return await _complementary_from_relations(conn, business_id, anchor_id, exclude, limit)
    return await _complementary_heuristic(conn, business_id, anchor_id, exclude, limit)


async def _has_complementary_relations(
    conn: asyncpg.Connection, business_id: str, anchor_id: str
) -> bool:
    """Ancora are ≥1 relație de complementaritate (complement/routine_next/accessory), indiferent
    de eligibilitatea produsului-țintă? Decide relations-first vs fallback heuristic (NU eligibi-
    litatea). Același set de `kind` ca `_complementary_from_relations`. Tenant-scoped (P7)."""
    return bool(
        await conn.fetchval(
            "select exists(select 1 from product_relations where business_id=$1 and product_id=$2 "
            "and kind in ('complement', 'routine_next', 'accessory'))",
            business_id,
            anchor_id,
        )
    )


async def _complementary_from_relations(
    conn: asyncpg.Connection,
    business_id: str,
    anchor_id: str,
    exclude: list[str],
    limit: int,
) -> list[dict[str, Any]]:
    """NX-171b: complementarele din `product_relations`. Agregă la UN rând per produs-înrudit
    (`min(prioritate)` peste kind-uri: routine_next < complement < accessory) → fără duplicat când
    un produs e legat prin >1 kind. Filtre de cumpărabilitate + published (per-tenant). `$1` =
    business_id (folosit ȘI de predicatul content_status)."""
    cs = _content_status_pred()
    sql = (
        _SELECT
        + " join (select related_id,"
        + "          min(case kind when 'routine_next' then 0 when 'complement' then 1 else 2 end)"
        + "            as prio,"
        + "          min(position) as pos"
        + "        from product_relations"
        + "        where business_id = $1 and product_id = $2"
        + "          and kind in ('complement', 'routine_next', 'accessory')"
        + "        group by related_id) pr on pr.related_id = p.id"
        + " where p.business_id = $1 and p.status = 'active'"
        + " and p.availability in ('in_stock', 'low_stock')"
        + " and p.id <> all($3::uuid[])"
        + (f" and {cs}" if cs else "")
        + f" order by pr.prio, pr.pos, {_SHRUNK_RATING} desc, p.id"
        + " limit $4"
    )
    rows = await conn.fetch(sql, business_id, anchor_id, exclude, limit)
    return [_row_to_product(r) for r in rows]


async def _complementary_heuristic(
    conn: asyncpg.Connection,
    business_id: str,
    anchor_id: str,
    exclude: list[str],
    limit: int,
) -> list[dict[str, Any]]:
    """Fallback heuristic (pre-171b): produse din ACELAȘI brand SAU care împart un `concern` cu
    ancora, dar dintr-o categorie DIFERITĂ (complement, NU substitut). Same-brand întâi, apoi rating
    shrunk. Folosit când ancora n-are relații explicite sau kill-switch-ul e OFF."""
    same_brand = "(select brand_id from products where business_id = $1 and id = $2)"
    # concern-urile ancorei ca text[] (gol → '{}' → fără overlap, cade pe same-brand). `?|` oricare.
    anchor_concerns = (
        "coalesce((select array(select jsonb_array_elements_text(pa.attributes->'concerns'))"
        "          from products pa where pa.business_id = $1 and pa.id = $2), '{}')::text[]"
    )
    cs = _content_status_pred()
    sql = (
        _SELECT
        + " where p.business_id = $1 and p.status = 'active'"
        + " and p.availability in ('in_stock', 'low_stock')"
        + " and p.id <> all($3::uuid[])"  # exclude ancora + ce e în coș
        # categorie DIFERITĂ (complement, NU substitut — alt ser nu „merge bine cu" un ser):
        + " and p.primary_category_id is distinct from"
        + "     (select primary_category_id from products where business_id = $1 and id = $2)"
        + f" and (p.brand_id = {same_brand} or (p.attributes->'concerns') ?| {anchor_concerns})"
        + (f" and {cs}" if cs else "")
        + f" order by (p.brand_id = {same_brand}) desc nulls last, {_SHRUNK_RATING} desc, p.id"
        + " limit $4"
    )
    rows = await conn.fetch(sql, business_id, anchor_id, exclude, limit)
    return [_row_to_product(r) for r in rows]


# --- traversarea grafului de relații (structură, nu prezentare) ---------------------------------
#
# Plasa de la marginea DB. Adâncimea e deja validată de `src/domain/relation_kinds.py`, dar query-ul
# nu se sprijină pe apelant: același idiom ca `limit = min(limit, 6)` din restul modulului. Un
# apelant care cere adâncime 50 primește 6, nu un query care cheltuie bugetul de tur (NX-241).
_MAX_RELATION_DEPTH = 6
# Plafon de rânduri. Cu grad de ieșire 3, adâncimea 4 poate atinge zeci de noduri; plafonul ține
# rezultatul mărginit fără să presupună forma grafului unui tenant anume.
_MAX_RELATION_HOPS = 60

# `cycle id set is_cycle using path` (SQL standard, Postgres 14+) mută detecția de ciclu în MOTOR:
# A→B→C→A devine un rând MARCAT, nu un query nemărginit. Schema 027 interzice self-relation, dar NU
# interzice ciclurile, iar pe date reale ele EXISTĂ (`complement` e ciclic pe toate ancorele:
# simetria e definiția relației, nu un defect de seed). Fără clauza asta, o buclă de date ar
# transforma o recomandare într-un timeout de tur.
#
# `path` acumulează DOAR `related_id`, deci ancora nu e în el: A→B→A nu se marchează ca ciclu la
# pasul 2. De aceea ancora se exclude EXPLICIT (`id <> $2`) — altfel produsul de la care am plecat
# s-ar putea întoarce în propria listă de recomandări.
_TRAVERSE_SQL = """
    with recursive reach as (
        select r.related_id  as id,
               1             as depth,
               r.position    as pos
          from product_relations r
         where r.business_id = $1 and r.product_id = $2::uuid and r.kind = $3
        union all
        select r.related_id,
               x.depth + 1,
               r.position
          from reach x
          join product_relations r
            on r.business_id = $1
           and r.product_id  = x.id
           and r.kind        = $3
         where x.depth < $4
    ) cycle id set is_cycle using path
    select id::text        as id,
           min(depth)      as depth,
           min(pos)        as position
      from reach
     where not is_cycle and id <> $2::uuid
     group by id
     order by min(depth), min(pos), id
     limit $5
"""


async def traverse_relations(
    conn: asyncpg.Connection,
    business_id: str,
    *,
    anchor_id: str,
    kind: str,
    max_depth: int,
    limit: int = _MAX_RELATION_HOPS,
) -> list[dict[str, Any]]:
    """**ACCESIBILITATE**, nu drum: produsele la care se poate ajunge din `anchor_id` urmând muchii
    de tipul `kind`, fiecare cu adâncimea la care e atins prima oară. UN singur query (NX-231),
    tenant-scoped în AMBII pași ai recursiei.

    Distincția din prima frază e esențială și e ușor de ratat. Cu grad de ieșire 3, produsul de la
    adâncimea 2 NU e neapărat succesorul celui de la adâncimea 1 — sunt ramuri diferite ale
    aceleiași explorări. Pentru un tip `bounded` (substitut tranzitiv) sau pentru vecini asta e fix
    ce trebuie. Pentru un tip `ordered` (o SECVENȚĂ de pași) ar produce o rutină cusută din bucăți:
    acolo se folosește `traverse_relation_chain`, care întoarce un drum real.

    Întoarce **REFERINȚE**, nu produse: `{id, depth, position}`, pe modelul portului de retrieval
    NX-238 (candidații sunt referințe, nu obiecte). Consecința practică: **structura nu se amestecă
    cu prezentarea.** Filtrele de cumpărabilitate (`status`, `availability`, `content_status`) se
    aplică de apelant, DUPĂ traversare, nu în recursie. Altfel un pas temporar fără stoc ar RUPE
    lanțul în loc să lipsească din el — exact distincția pe care `get_complementary_products` o face
    deja între „ancora are relații" și „țintele sunt eligibile".

    `max_depth` e plafonat la `_MAX_RELATION_DEPTH`; `max_depth <= 1` întoarce vecinii direcți (un
    tip nedeclarat primește exact asta din registru, deci comportamentul de azi).
    `business_id = $1` (izolare P7; RLS plasă). Hidratarea se face cu `get_products_by_ids`."""
    depth = max(1, min(int(max_depth), _MAX_RELATION_DEPTH))
    rows = await conn.fetch(
        _TRAVERSE_SQL,
        business_id,
        anchor_id,
        kind,
        depth,
        max(1, min(int(limit), _MAX_RELATION_HOPS)),
    )
    return [{"id": r["id"], "depth": int(r["depth"]), "position": int(r["position"])} for r in rows]


# DRUMUL, nu frontiera. `row_number() over (partition by parent ...)` păstrează UN singur succesor
# per nod — cel mai bun după poziția curată a muchiei, apoi id (determinist). Din anchor, mulțimea
# rezultată E chiar lanțul: un nod, succesorul lui, succesorul aceluia. Fără partiționarea asta ar
# trebui să întoarcem frontiera întreagă (3 + 9 + 27 + 81 la grad 3 și adâncime 4), iar plafonul de
# rânduri ar tăia exact nivelurile de jos, adică fix acolo unde lanțul trebuie să ajungă.
#
# `parent` iese în rezultat pentru ca ordonarea să fie VERIFICABILĂ de apelant: un lanț se poate
# valida ca drum (fiecare pas e copilul celui dinainte), nu doar presupune din adâncime.
_TRAVERSE_CHAIN_SQL = """
    with recursive reach as (
        select r.related_id  as id,
               r.product_id  as parent,
               1             as depth,
               r.position    as pos
          from product_relations r
         where r.business_id = $1 and r.product_id = $2::uuid and r.kind = $3
        union all
        select r.related_id,
               x.id,
               x.depth + 1,
               r.position
          from reach x
          join product_relations r
            on r.business_id = $1
           and r.product_id  = x.id
           and r.kind        = $3
         where x.depth < $4
    ) cycle id set is_cycle using path
    , best as (
        select id, parent, depth, pos,
               row_number() over (partition by parent order by pos, id) as rn
          from reach
         where not is_cycle and id <> $2::uuid
    )
    select id::text     as id,
           parent::text as parent,
           depth        as depth,
           pos          as position
      from best
     where rn = 1
     order by depth, pos, id
"""


async def traverse_relation_chain(
    conn: asyncpg.Connection,
    business_id: str,
    *,
    anchor_id: str,
    kind: str,
    max_depth: int,
) -> list[dict[str, Any]]:
    """**DRUMUL** de la `anchor_id`, urmând la fiecare pas cel mai bun succesor. UN singur query.

    Pentru un tip `ordered` (o secvență: curățare → tonic → tratament → hidratare), asta e ce
    trebuie. `traverse_relations` ar da accesibilitatea, iar produsul de la adâncimea 2 ar putea fi
    succesorul altei ramuri decât pasul ales la adâncimea 1 — o rutină cusută din bucăți, corectă
    pe cifre și falsă ca sfat.

    Întoarce referințe `{id, parent, depth, position}` în ordinea pașilor, cel mult `max_depth`
    intrări. Ca și la `traverse_relations`, filtrele de cumpărabilitate sunt ale apelantului: un pas
    fără stoc lipsește din prezentare, nu rupe structura. Gol = ancora n-are lanț de tipul ăsta.
    `business_id = $1` (izolare P7; RLS plasă)."""
    depth = max(1, min(int(max_depth), _MAX_RELATION_DEPTH))
    rows = await conn.fetch(_TRAVERSE_CHAIN_SQL, business_id, anchor_id, kind, depth)
    return [
        {
            "id": r["id"],
            "parent": r["parent"],
            "depth": int(r["depth"]),
            "position": int(r["position"]),
        }
        for r in rows
    ]


async def list_category_slugs(conn: asyncpg.Connection, business_id: str) -> list[str]:
    """Slug-urile categoriilor SERVABILE ale tenantului — pentru groundarea triajului.

    Triaj-ul (nano) primește lista asta și alege `category_key` din ea; orice
    valoare inventată în afara listei e respinsă în cod (→ category_key None /
    CLARIFY). `conn` trebuie să fie deja tenant-scoped (tenant_conn).

    „Servabil" = are cel puțin un produs în subarbore, prin `servable_count_sql` (aceeași definiție
    ca vocabularul, deliberat — vezi `src/catalog/vocabulary.py`). Înainte, validarea verifica doar
    că rândul EXISTĂ în `categories`, ceea ce lăsa să treacă rafturi goale: pe demo, 60 din 102
    categorii n-aveau niciun produs, iar un `category_key` ales dintre ele producea garantat zero
    rezultate — indistinct, pentru straturile de deasupra, de „catalogul chiar n-are asta"."""
    rows = await conn.fetch(
        f"select c.slug from categories c"
        f" where c.business_id = $1 and {servable_count_sql('c')} > 0"
        f" order by c.slug",
        business_id,
    )
    return [r["slug"] for r in rows]


async def sibling_categories(
    conn: asyncpg.Connection, business_id: str, slug: str, *, limit: int = 4
) -> list[str]:
    """NX-136: numele categoriilor SURORI (același `parent_id`) ale categoriei cu `slug`, pentru
    chips-urile de închidere („recomandă un gel de curățare" după o cremă). Dublu-scoped pe
    `business_id` (c1 ȘI c2, P7). `is not distinct from` tratează `parent_id` NULL = NULL → o
    categorie TOP-LEVEL primește celelalte top-level ca surori. `conn` tenant-scoped."""
    rows = await conn.fetch(
        """
        select c2.name
        from categories c1
        join categories c2
          on c2.business_id = c1.business_id
         and c2.parent_id is not distinct from c1.parent_id
         and c2.id <> c1.id
        where c1.business_id = $1 and c1.slug = $2
        order by c2.name
        limit $3
        """,
        business_id,
        slug,
        limit,
    )
    return [r["name"] for r in rows]


async def list_category_names(conn: asyncpg.Connection, business_id: str) -> list[str]:
    """Numele categoriilor SERVABILE ale tenantului — pentru groundarea promptului agentului
    (NX-78, principiul 9). `order by name` → ordine deterministă (prefix de cache stabil).
    `conn` trebuie să fie deja tenant-scoped (tenant_conn).

    Două schimbări față de varianta inițială, ambele din același incident măsurat:

    1. **Doar ce e servabil.** Promptul lista tot tabelul, deci îi spunea modelului „vinzi din
       categoria Ten" când «Ten» n-avea niciun produs. Modelul alegea cuminte raftul gol pe care
       i-l arătasem noi, iar filtrul dur pe categorie transforma asta în zero rezultate și într-un
       „n-am găsit" fals. Nu anunța ce nu poți servi.
    2. **Toate nivelurile, nu doar rădăcinile.** Cu doar 15 rădăcini, o cerere de cremă de față
       trebuia ghicită ca părinte; cu frunzele disponibile, modelul poate numi «Creme hidratante»
       direct, iar o cerere de cremă nu mai poate ateriza pe măști. Costul e câteva sute de tokeni
       într-un prefix oricum cache-uit."""
    rows = await conn.fetch(
        f"select c.name from categories c"
        f" where c.business_id = $1 and {servable_count_sql('c')} > 0"
        f" order by c.name",
        business_id,
    )
    return [r["name"] for r in rows]


async def list_routing_aliases(
    conn: asyncpg.Connection, business_id: str, *, limit: int = 20
) -> list[tuple[str, str]]:
    """Aliasele de rutare APROBATE (`(phrase_norm, target_value)`) — hint scurt în promptul
    agentului (NX-78). DOAR `status='approved'` (principiul 9: nu rutăm pe ghicit neaprobat).
    `order by phrase_norm` → deterministic (prefix de cache stabil)."""
    rows = await conn.fetch(
        "select phrase_norm, coalesce(target_value, '') as target "
        "from intent_aliases "
        "where business_id = $1 and status = 'approved' "
        "order by phrase_norm limit $2",
        business_id,
        limit,
    )
    return [(r["phrase_norm"], r["target"]) for r in rows]


# ── NX-234: rehidratarea contextului de pagină (UN round-trip, tenant-scoped) ───────────────
# De ce un query separat și nu `get_products_by_ids`: contextul aduce entități de TIPURI diferite
# (produse + o variantă + o categorie) și referințe care pot fi UUID-ul nostru SAU cheia proprie a
# platformei magazinului (`external_id` / `sku` / `slug` — singurele pe care pagina gazdă le
# cunoaște). Un lookup per entitate ar fi N+1 exact pe calea sincronă a unui turn; aici e un
# UNION ALL: 1 round-trip pentru 1 ref sau pentru 10.
#
# `data` e jsonb ca să încapă trei forme în aceeași coloană. Costul (serializare) e plătit o
# singură dată per turn și cumpără proprietatea care contează: numărul de query-uri nu depinde de
# numărul de referințe.

_CONTEXT_PRODUCT_JSON = f"""
    jsonb_build_object(
        'id', p.id::text,
        'external_id', p.external_id,
        'name', p.name,
        'brand', b.name,
        'url', p.product_url,
        'image', img.url,
        'currency', p.currency,
        'price', {_EFFECTIVE_PRICE}::float8,
        'list_price', (case when {_SALE_ACTIVE} then p.price end)::float8,
        'price_source', (case when vp.price is not null then 'variant_min' else 'product' end),
        'availability', p.availability,
        'stock_total', p.stock_total,
        'rating', p.rating::float8,
        'review_count', p.review_count,
        'review_summary', prs.summary,
        'category_id', p.primary_category_id::text,
        'category_name', c.name,
        'category_slug', c.slug,
        'category_path', c.path,
        'delivery_class', p.delivery_class,
        'restock_date', p.restock_date,
        'content_status', p.content_status,
        'updated_at', p.updated_at,
        'synced_at', p.synced_at,
        'verified_at', p.verified_at
    )
"""

_CONTEXT_VARIANT_JSON = f"""
    jsonb_build_object(
        'id', v.id::text,
        'product_id', v.product_id::text,
        'external_id', v.external_id,
        'label', v.label,
        'sku', v.sku,
        'price', (case when {_VARIANT_SALE_ON} then v.sale_price else v.price end)::float8,
        'list_price', (case when {_VARIANT_SALE_ON} then v.price end)::float8,
        'stock', v.stock,
        'updated_at', v.updated_at
    )
"""

_CONTEXT_CATEGORY_JSON = """
    jsonb_build_object(
        'id', c.id::text,
        'name', c.name,
        'slug', c.slug,
        'path', c.path,
        'parent_id', c.parent_id::text,
        'updated_at', c.updated_at
    )
"""


async def load_context_entities(
    conn: asyncpg.Connection,
    business_id: str,
    *,
    product_uuids: list[str] | None = None,
    product_keys: list[str] | None = None,
    variant_uuids: list[str] | None = None,
    variant_keys: list[str] | None = None,
    category_uuids: list[str] | None = None,
    category_keys: list[str] | None = None,
) -> list[dict[str, Any]]:
    """Rehidratează contextul de pagină într-UN singur round-trip. `business_id = $1` pe FIECARE
    ramură (P7) — un id existent la ALT tenant nu se întoarce, deci e indistinct de inexistent.

    Referințele vin în două forme, pentru că pagina gazdă cunoaște cheia platformei ei, nu UUID-ul
    nostru: `*_uuids` = `id`-ul canonic; `*_keys` = cheia proprie a tenantului (`products
    .external_id`, `product_variants.external_id`/`sku`, `categories.slug`) — toate unice pe
    business în schema reală. `ref` (coloana întoarsă) e cheia PE CARE S-A CERUT, ca apelantul să
    poată mapa înapoi fără al doilea query.

    Produsele trec prin ACELEAȘI porți ca discovery-ul (`status='active'` + quality gate
    per-tenant): un produs nepublicat nu devine „evidence" doar fiindcă browserul a afirmat că
    e pe ecran. Variantele/categoriile nu au poartă proprie — validarea lor de relație (varianta
    aparține produsului) se face în `src/catalog/context_resolver.py`, cu datele de aici."""
    branches: list[str] = []
    params: list[Any] = [business_id]

    def placeholder(value: Any) -> str:
        params.append(value)
        return f"${len(params)}"

    cs = _content_status_pred()
    product_gate = " and p.status = 'active'" + (f" and {cs}" if cs else "")
    # `vp` = minimul pe variante, cu fereastra de promoție a PRODUSULUI (varianta o moștenește —
    # vezi `_VARIANT_SALE_ON`); `img` = prima poză. Aceleași laterale ca `_SELECT`, ca prețul de
    # context să fie EXACT prețul pe care îl vede validatorul pe orice altă cale.
    product_from = (
        " from products p"
        " left join brands b on b.id = p.brand_id"
        " left join categories c on c.id = p.primary_category_id"
        " left join product_review_summaries prs on prs.product_id = p.id"
        " left join lateral (select min(case when "
        + _SALE_WINDOW_OK
        + " and v.sale_price is not null and v.sale_price < v.price"
        " then v.sale_price else v.price end) as price from product_variants v"
        " where v.product_id = p.id and v.business_id = p.business_id) vp on true"
        " left join lateral (select pi.url from product_images pi where pi.product_id = p.id"
        " order by pi.position asc nulls last limit 1) img on true"
    )
    # Varianta se citește cu produsul ei alături (`p`), fiindcă fereastra de promoție e a
    # produsului; join-ul e scopat pe AMBELE capete (P7), nu doar pe `v.business_id`.
    variant_from = (
        " from product_variants v"
        " join products p on p.id = v.product_id and p.business_id = v.business_id"
    )
    if product_uuids:
        branches.append(
            f"select 'product' as kind, p.id::text as ref, {_CONTEXT_PRODUCT_JSON} as data"
            + product_from
            + f" where p.business_id = $1 and p.id = any({placeholder(product_uuids)}::uuid[])"
            + product_gate
        )
    if product_keys:
        branches.append(
            f"select 'product' as kind, p.external_id as ref, {_CONTEXT_PRODUCT_JSON} as data"
            + product_from
            + " where p.business_id = $1 and p.external_id = any("
            + placeholder(product_keys)
            + "::text[])"
            + product_gate
        )
    if variant_uuids:
        branches.append(
            f"select 'variant' as kind, v.id::text as ref, {_CONTEXT_VARIANT_JSON} as data"
            + variant_from
            + f" where v.business_id = $1 and v.id = any({placeholder(variant_uuids)}::uuid[])"
        )
    if variant_keys:
        vk = placeholder(variant_keys)
        branches.append(
            # `ref` = cheia PE CARE S-A CERUT, nu prima coloană ne-nulă: o variantă cu
            # `external_id` diferit de `sku` s-ar întoarce sub o cheie pe care apelantul n-a
            # cerut-o, deci ar arăta ca „negăsită".
            f"select 'variant' as kind,"
            f" (case when v.external_id = any({vk}::text[]) then v.external_id else v.sku end)"
            f" as ref, {_CONTEXT_VARIANT_JSON} as data"
            + variant_from
            + f" where v.business_id = $1 and (v.external_id = any({vk}::text[])"
            f" or v.sku = any({vk}::text[]))"
        )
    if category_uuids:
        branches.append(
            f"select 'category' as kind, c.id::text as ref, {_CONTEXT_CATEGORY_JSON} as data"
            " from categories c"
            f" where c.business_id = $1 and c.id = any({placeholder(category_uuids)}::uuid[])"
        )
    if category_keys:
        branches.append(
            f"select 'category' as kind, c.slug as ref, {_CONTEXT_CATEGORY_JSON} as data"
            " from categories c"
            f" where c.business_id = $1 and c.slug = any({placeholder(category_keys)}::text[])"
        )
    if not branches:
        return []
    rows = await conn.fetch(" union all ".join(branches), *params)
    out: list[dict[str, Any]] = []
    for r in rows:
        data = r["data"]
        if isinstance(data, str):
            try:
                data = json.loads(data)
            except (ValueError, TypeError):
                continue
        out.append({"kind": r["kind"], "ref": r["ref"], "data": data or {}})
    return out


async def search_products_semantic(
    conn: asyncpg.Connection,
    business_id: str,
    query_embedding: list[float],
    *,
    price_max: float | None = None,
    concerns: list[str] | None = None,
    facet_filters: Mapping[str, Sequence[str]] | None = None,
    features: list[str] | None = None,
    searchable_facets: tuple[str, ...] = (),
    variant_label: str | None = None,
    category: str | Sequence[str] | None = None,
    brand: str | None = None,
    sort_mode: str = "relevance",
    in_stock_only: bool = False,
    limit: int = 6,
    pool: int | None = None,
    embedding_doc_type: str | None = None,
) -> list[dict[str, Any]]:
    """Căutare HIBRIDĂ: filtre SQL dure (preț/categorie/brand/concerns/stoc) + ranking.
    `query_embedding` = vectorul mesajului (calculat de tool/agent prin adaptor — stratul de date
    NU apelează LLM). `conn` trebuie tenant-scoped (tenant_conn).

    `sort_mode`: `relevance` = cosine (cel mai apropiat primul); `price_asc`/`rating_desc` = sort
    explicit pe subsetul filtrat semantic. `concerns` filtrează pe `attributes->'concerns'`.

    `pool` (NX-113b): când e dat, întoarce ~`pool` candidați pentru fuziunea RRF (nu doar 6);
    poziția în listă = rangul vectorial. Lipsă (`None`) → comportament compat (max 6).

    NX-113c: `query_embedding` se trimite ca `list[float]` DIRECT (codecul pgvector din pool îl
    encodează) — fără literalul text de ~15KB inline pe hot path. SELECT-ul expune și
    `cosine_distance` (distanța vectorială a rândului) ca semnal de calitate (`top_cosine_distance`
    în emit)."""
    sql_limit = pool if pool is not None else min(limit, 6)

    conds = ["p.business_id = $1", "p.status = 'active'"]
    params: list[Any] = [business_id]

    def placeholder(value: Any) -> str:
        params.append(value)
        return f"${len(params)}"

    qvec_ph = placeholder(query_embedding)  # vectorul de query (list[float], codec pgvector)
    if price_max is not None:
        conds.append(f"{_EFFECTIVE_PRICE} <= {placeholder(price_max)}")
    if category:
        conds.append(_category_clause(category, placeholder))  # NX-167 (A): match pe arbore
    if brand:
        # Filtru DUR pe brand (la fel ca SQL-only): un brand cerut care nu există în catalog →
        # zero rezultate, NU produse semantic-apropiate de la alt brand (bug-ul „avem … Chanel").
        # NX-178: și brandul se caută fără diacritice („petala" → „Petala", „loreal" → „L'Oréal")
        conds.append(f"ro_unaccent(b.name) like ro_unaccent({placeholder(f'%{brand}%')})")
    if concerns:
        conds.append(f"(p.attributes->'concerns') ?| {placeholder(concerns)}::text[]")
    if facet_filters and (fc := _facet_filter_clause(facet_filters, placeholder)):
        conds.append(fc)
    if features and searchable_facets:
        conds.append(_feature_clause(searchable_facets, features, placeholder))
    if variant_label:  # NX-135: filtru DUR pe eticheta de variantă (fallback gradat)
        conds.append(_variant_label_clause(variant_label, placeholder))
    if in_stock_only:
        conds.append("p.availability in ('in_stock', 'low_stock')")
    if cs := _content_status_pred():  # NX-171c: doar 'published' (per-tenant, gated)
        conds.append(cs)

    # NX-171d: embeddings versionate (PK compus product_id, doc_type, model). Join-ul TREBUIE să
    # filtreze doc_type + model activ, altfel >1 rând/produs → produs duplicat în rezultate. +
    # `pe.business_id = p.business_id` (P7: un rând embedding cu business_id greșit nu scapă).
    emb_doc = placeholder(semantic_embedding_doc_type(embedding_doc_type))
    emb_model = placeholder(get_settings().model_embed)
    # Injectează coloana distanței vectoriale (cosine) în SELECT — semnal de calitate pt emit.
    cos_col = f"        (pe.embedding <=> {qvec_ph}::vector)::float8 as cosine_distance,\n"
    select_with_cos = _SELECT.replace("    select\n", "    select\n" + cos_col, 1)
    sql = (
        select_with_cos
        + " join product_embeddings pe on pe.product_id = p.id"
        + f"   and pe.business_id = p.business_id and pe.doc_type = {emb_doc}"
        + f"   and pe.model = {emb_model}"
        + " where "
        + " and ".join(conds)
        + _order_clause(sort_mode, qvec_ph=qvec_ph)
        + f" limit {placeholder(sql_limit)}"
    )
    rows = await conn.fetch(sql, *params)
    return [_row_to_product(r) for r in rows]
