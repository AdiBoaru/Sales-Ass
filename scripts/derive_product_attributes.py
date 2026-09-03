"""Derivă atributele STRUCTURATE ale produselor din secțiunile de catalog. Dry-run implicit.

De ce există: pachetul de domeniu al tenantului declară 9 fațete, dar `facet_coverage` măsoară 0%
pe șase dintre ele (`concerns`, `skin_type`, `key_ingredients`, `spf`, `fragrance_free`, `texture`).
Vocabularul EXISTĂ (87 de fraze → 20 de chei canonice, derivate din 12.665 de fraze reale de
căutare), dar niciun produs nu poartă valorile, deci fiecare filtru pe nevoi rezolvă azi
`UNKNOWN(overlay_target_dead)`: clientul spune „ten gras", sistemul îl aude, îl ține minte, și nu
poate face nimic cu el.

Sursa derivării NU e numele plus descrierea. Măsurat pe catalogul SOLE, „ten gras" apare la 26 de
produse în `name || description` și la ~1.078 în secțiunile `aura` (`fit`/`problem`/
`recommendation_trigger`). Descrierea e textul de magazin; nevoia e scrisă în secțiunile semantice.
De 40 de ori mai mult semnal, în același rând de produs.

Capcana pe care o evită explicit: `anti_fit` spune pentru CINE NU e produsul („eviti daca ai ten
gras"). O potrivire de frază acolo ar produce exact eticheta inversă, iar rezultatul ar fi un filtru
care recomandă produsul greșit exact clientului care l-a exclus. Deci secțiunile negative nu intră
în textul pozitiv, niciodată.

Fiecare valoare derivată poartă provenance: regula care a produs-o (`rule_id`) și secțiunile din
care a ieșit (`derived_from`). O valoare fără sursă nu se scrie — nu ca disciplină, ci pentru că
tabela de fapte e ce verifică validatorul (stagiul 8) și `grounding_guard` (NX-240) în aval: un
atribut greșit AICI nu mai e prins de nimeni și iese la client ca afirmație.

NX-268 — trei schimbări față de prima versiune, toate pe măsurătoare:

**Potrivirea tolerează flexiunea, dar NU renunță la frază.** Regula trăiește acum în
`src/catalog/derivation.py` (pură, testabilă fără DB) și e explicată acolo: măsurat, stemurile
dintr-un singur cuvânt din care venea cifra „93 → 1.120" pică testul de discriminare (`gras` prinde
„acizi grași"), iar fraza cu tokeni pe prefix câștigă 1,23× fără nicio pierdere.

**`key_ingredients` nu mai e o listă de propoziții.** 99,1% acoperire cu 10.392 de valori distincte
nu e o fațetă, e text. Capul liniei devine valoarea canonică, vocabularul se plafonează, restul
rămâne text (unde era oricum util).

**Scrierea există, dar cere `--apply`.** Idempotentă prin cheia unică a tabelei; o a doua rulare nu
schimbă niciun rând. Dry-run rămâne implicit.

    python scripts/derive_product_attributes.py --business <uuid>             # dry-run + raport
    python scripts/derive_product_attributes.py --business <uuid> --sample 12 # + exemple citate
    python scripts/derive_product_attributes.py --business <uuid> --propose-stems
    python scripts/derive_product_attributes.py --business <uuid> --apply     # scrie (confirmă)
"""

from __future__ import annotations

import argparse
import asyncio
import collections
import json
import pathlib
import re
import sys
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from src.catalog.derivation import (  # noqa: E402
    IngredientVocabulary,
    build_matchers,
    ingredient_head,
    match_keys,
    phrase_span,
    signal_name,
    tokens,
)
from src.catalog.query_terms import stopwords  # noqa: E402
from src.db.connection import close_pool, tenant_conn  # noqa: E402
from src.db.queries.businesses import load_business  # noqa: E402
from src.domain.loader import load_domain_pack  # noqa: E402
from src.domain.normalize import normalize  # noqa: E402

# Secțiunile care spun pentru CINE e produsul. `anti_fit` lipsește deliberat (spune pentru cine NU
# e), la fel `storage`/`usage`/`composition` — sunt instrucțiuni, nu nevoi. `questions` și `summary`
# repetă nevoia în cuvintele clientului, deci ajută recall-ul fără să schimbe sensul.
POSITIVE_SECTIONS = (
    "fit",
    "problem",
    "purpose",
    "recommendation_trigger",
    "summary",
    "questions",
    "editorial",
)

# Secțiunile din care se citește formula — separat, fiindcă o afirmație despre compoziție („fără
# X") nu e o nevoie a cumpărătorului.
FORMULA_SECTIONS = ("composition", "key_ingredients")


def _value_patterns(spec: Mapping[str, Any]) -> dict[str, tuple[re.Pattern[str], ...]]:
    """Valorile unei fațete DECLARATE în pachet → tiparele care le recunosc în text.

    Aici a fost, până la NX-264, un dicționar `TEXTURE_TERMS` cu „crema"/„ser"/„gel" scris direct în
    cod: cuvinte de beauty, în românește, dictate de mine. Pe alt catalog erau zgomot mort, iar pe
    al nostru erau o fațetă pe care n-a produs-o catalogul.

    Acum valorile vin din fațeta tenantului, iar sinonimele fiecăreia din `aliases`. Fațeta fără
    valori nu se derivă — se raportează ca nedeclarată, ca să se vadă că lipsește, nu ca să fie
    presupusă. Propunerile se obțin cu `scripts/facet_discovery.py` și le ratifică un om, o dată per
    tenant."""
    out: dict[str, tuple[re.Pattern[str], ...]] = {}
    for value in spec.get("values") or ():
        forms = [str(value)] + [str(a) for a in (spec.get("aliases") or {}).get(value, ())]
        pats = tuple(re.compile(rf"(?<!\w){re.escape(normalize(f))}(?!\w)") for f in forms if f)
        if pats:
            out[str(value)] = pats
    return out


def _claim_patterns(
    spec: Mapping[str, Any],
) -> tuple[re.Pattern[str] | None, re.Pattern[str] | None]:
    """(afirmă, contrazice) pentru o fațetă de tip PROMISIUNE, din pachet.

    „Fără parfum" e o promisiune, deci pragul e precizia, nu recall-ul: se acceptă doar formularea
    afirmativă explicită, iar orice mențiune contrară în același text o anulează. Frazele sunt însă
    ale limbii și ale verticalului, deci stau în pachet (`claim_affirms` / `claim_denies`), nu
    într-un `re.compile` din cod — a doua scurgere pe care NX-264 o închide."""
    affirms = [normalize(str(x)) for x in (spec.get("claim_affirms") or ()) if str(x).strip()]
    denies = [normalize(str(x)) for x in (spec.get("claim_denies") or ()) if str(x).strip()]
    yes = re.compile("|".join(re.escape(a) for a in affirms)) if affirms else None
    no = re.compile("|".join(re.escape(d) for d in denies)) if denies else None
    return yes, no


SPF_RE = re.compile(r"\bspf\s*([0-9]{1,2})\s*\+?")


@dataclass
class FacetTally:
    """Ce a produs o fațetă pe tot catalogul, plus de unde. `sources` există ca să se poată spune
    care secțiune a purtat semnalul — dacă 95% dintr-o fațetă vine dintr-o singură secțiune,
    fațeta aia depinde de un singur furnizor de conținut, iar asta e o fragilitate de raportat."""

    products: set[str] = field(default_factory=set)
    values: collections.Counter = field(default_factory=collections.Counter)
    sources: collections.Counter = field(default_factory=collections.Counter)


def _clean_lines(body: str) -> list[str]:
    """Bullet-urile unei secțiuni, fără resturile de UI ale paginii sursă."""
    out = []
    for raw in body.splitlines():
        line = raw.strip().lstrip("•-*0123456789. ").strip(" „”\"'")
        if not line or normalize(line) in {"vezi mai multe detalii", "ascunde"}:
            continue
        out.append(line)
    return out


# --- scrierea (idempotentă, cere --apply) ------------------------------------------------------

# Regula e VERSIONATĂ: „regula R s-a dovedit greșită" trebuie să se poată repara global
# (`where rule_id = …`), nu produs cu produs. O schimbare de mecanism de potrivire e o versiune
# nouă, nu o rescriere tăcută a acelorași rânduri.
RULE_NEEDS = "pack.phrase_prefix.v2"
RULE_INGREDIENTS = "section.key_ingredients.head.v2"
RULE_SPF = "regex.spf.v1"

# Câte produse într-o tranzacție. Un lot prea mare ține un lock lung pe `products`; unul prea mic
# înmulțește round-trip-urile. 200 e compromisul obișnuit pe pooler.
BATCH = 200

_UPSERT_SIGNAL = """
insert into product_derived_signals
       (business_id, product_id, signal, derived_from, rule_id, locale)
values ($1, $2::uuid, $3, $4::text[], $5, $6)
on conflict (business_id, product_id, signal, rule_id, locale) do update
   set derived_from = excluded.derived_from
 where product_derived_signals.derived_from is distinct from excluded.derived_from
"""

# Proiecția în `products.attributes` — ce citește read-path-ul AZI (fațetele NX-186 au
# `source: attribute`). Sursa rămâne `product_derived_signals`; asta e o copie derivată din ea,
# scrisă în aceeași tranzacție, ca cele două să nu poată diverge.
#
# `jsonb_strip_nulls` NU se folosește: o cheie pusă pe `null` ar însemna „am derivat null", ceea ce
# nu e o valoare. Cheile pe care derivarea nu le produce se ȘTERG, ca UNKNOWN să rămână UNKNOWN
# (D7) în loc să rămână o valoare veche dintr-o rulare anterioară.
_PROJECT_ATTRS = """
update products
   set attributes = (coalesce(attributes, '{}'::jsonb) - $3::text[]) || $4::jsonb
 where business_id = $1 and id = $2::uuid
   and attributes is distinct from ((coalesce(attributes, '{}'::jsonb) - $3::text[]) || $4::jsonb)
"""


async def _write_batch(
    conn,
    business_id: str,
    locale: str,
    rows: list[tuple[str, dict[str, dict]]],
    facet_keys: list[str],
) -> tuple[int, int, list[str]]:
    """Scrie un lot ÎNTR-O tranzacție: semnalele + proiecția. → (semnale, produse atinse, sărite).

    Semnalele și proiecția stau în aceeași tranzacție deliberat: dacă proiecția ar fi separată, o
    cădere între ele ar lăsa `products.attributes` să contrazică tabela de fapte, iar
    `grounding_guard` ar verifica afirmațiile față de copia greșită.

    Fiecare produs are însă SAVEPOINT-ul lui. În Postgres, un statement care crapă abortează toată
    tranzacția, deci fără savepoint un singur produs cu o secțiune stricată ar anula lotul întreg —
    și, la 200 de produse pe lot, ar putea bloca derivarea la nesfârșit pe același rând. Cu
    savepoint, produsul e SĂRIT, numărat și raportat, iar restul lotului trece."""
    signals = touched = 0
    skipped: list[str] = []
    async with conn.transaction():
        for product_id, derived in rows:
            try:
                async with conn.transaction():  # savepoint (tranzacție imbricată în asyncpg)
                    attrs = {}
                    for facet, info in derived.items():
                        values = info["values"]
                        attrs[facet] = (
                            values if len(values) > 1 or facet in _LIST_FACETS else values[0]
                        )
                        for value in values:
                            status = await conn.execute(
                                _UPSERT_SIGNAL,
                                business_id,
                                product_id,
                                signal_name(facet, value),
                                info["derived_from"],
                                info["rule_id"],
                                locale,
                            )
                            signals += int(status.split()[-1])
                    status = await conn.execute(
                        _PROJECT_ATTRS, business_id, product_id, facet_keys, json.dumps(attrs)
                    )
                    touched += int(status.split()[-1])
            except Exception as e:  # noqa: BLE001 — un produs stricat nu oprește lotul
                skipped.append(product_id)
                print(f"  ! sărit {product_id}: {type(e).__name__}", file=sys.stderr)
    return signals, touched, skipped


# Fațetele care sunt LISTE prin contract (registrul NX-186 le declară `value_type: list`), deci
# rămân liste chiar cu o singură valoare. O fațetă-listă scrisă ca scalar ar face filtrul
# `attributes->'concerns' ?| …` să nu se mai potrivească — și n-ar da nicio eroare.
_LIST_FACETS = frozenset({"concerns", "key_ingredients"})


# --- propunerea de stemuri (read-only, nu scrie în pachet) --------------------------------------

# Cele trei porți, declarate ÎNAINTE de a privi rezultatul (ca la NX-246 felia 3). Sunt aceleași
# întrebări pe care le pune `facet_discovery.py` unei valori de fațetă, puse unui stem:
MIN_STEM_LEN = 5  # sub asta prefixul nu mai e o rădăcină, e o coincidență de litere
MIN_STEM_SUPPORT = 10  # sub asta nu se poate judeca nimic
MIN_STEM_LIFT = 4.0  # cât de concentrat e stemul pe produsele cheii, față de rata ei de bază
MAX_STEM_GROWTH = 2.0  # o lărgire MORFOLOGICĂ nu poate mai mult decât să dubleze setul


def _propose_stems(products, by_product, concern_map: dict[str, str], total: int) -> None:
    """Propune stemuri dintr-un cuvânt pentru `concern_stems`, cu cifrele fiecărei porți.

    Nu scrie nimic și nu ratifică nimic: un stem intră în pachet doar după ce un om se uită la ce
    prinde. Motivul e măsurat, nu principial — pe catalogul SOLE, `gras` (stemul din care venea
    cifra „93 → 1.120" a cardului) PICĂ poarta de lift, fiindcă prinde „acizi grași" din orice
    compoziție, iar `pielii` ar fi urcat `barrier` de 4,8× fără să însemne nimic."""
    texts = {}
    for p in products:
        secs = by_product.get(p["id"], {})
        body = " ".join(b for k in POSITIVE_SECTIONS for b in secs.get(k, []))
        texts[p["id"]] = tokens(normalize(body))

    by_key: dict[str, list[str]] = collections.defaultdict(list)
    for phrase, key in concern_map.items():
        by_key[key].append(normalize(phrase))

    print("\n--- propuneri de stemuri (NEratificate) ---")
    print(f"porți: len≥{MIN_STEM_LEN}, suport≥{MIN_STEM_SUPPORT}, lift≥{MIN_STEM_LIFT}, ")
    print(f"       creștere≤{MAX_STEM_GROWTH}× față de fraze")
    for key in sorted(by_key):
        phrase_toks = [tokens(p) for p in by_key[key]]
        base_set = {
            pid for pid, ws in texts.items() if any(phrase_span(ws, pt) for pt in phrase_toks)
        }
        if not base_set:
            continue
        base_rate = len(base_set) / total if total else 0
        candidates = {t for pt in phrase_toks for t in pt if len(t) >= MIN_STEM_LEN}
        kept: list[tuple[str, int, float]] = []
        for token in sorted(candidates):
            for cut in range(len(token), MIN_STEM_LEN - 1, -1):
                stem = token[:cut]
                hit = {pid for pid, ws in texts.items() if any(w.startswith(stem) for w in ws)}
                if len(hit) < MIN_STEM_SUPPORT or len(hit) > MAX_STEM_GROWTH * len(base_set):
                    continue
                lift = (len(hit & base_set) / len(hit)) / base_rate if base_rate else 0
                if lift >= MIN_STEM_LIFT:
                    kept.append((stem, len(hit - base_set), round(lift, 1)))
                    break
        if kept:
            shortest = []
            for stem, extra, lift in sorted(kept, key=lambda x: len(x[0])):
                if not any(stem.startswith(s) for s, _, _ in shortest):
                    shortest.append((stem, extra, lift))
            joined = ", ".join(f"{s} (+{e}, lift {ell})" for s, e, ell in shortest)
            print(f"  {key:20} {joined}")


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--business", required=True)
    ap.add_argument("--sample", type=int, default=0, help="câte produse să afișeze cu citate")
    ap.add_argument("--out", default=None, help="unde scrie raportul JSON")
    ap.add_argument(
        "--ingredient-vocab",
        type=int,
        default=250,
        help="câte ingrediente canonice (peste plafon rămân TEXT, nu fațetă)",
    )
    ap.add_argument(
        "--propose-stems",
        action="store_true",
        help="propune stemuri dintr-un cuvânt pentru `concern_stems`, cu cifrele porților",
    )
    ap.add_argument(
        "--apply", action="store_true", help="chiar scrie în catalog (fără el: doar raportează)"
    )
    args = ap.parse_args()

    try:
        async with tenant_conn(args.business) as conn:
            business = await load_business(conn, args.business)
            if business is None:
                print("business inexistent", file=sys.stderr)
                return 2
            pack = load_domain_pack(business)
            if pack is None:
                print("tenantul n-are domain pack", file=sys.stderr)
                return 2
            concern_map = dict(pack.concern_map)
            facet_values = {f.key: set(f.values or ()) for f in pack.facets}
            # Fațetele care se citesc din NUME și cele de tip promisiune, amândouă DECLARATE în
            # pachet. Ce nu e declarat nu se derivă: raportul spune care lipsesc, ca absența să se
            # vadă. Propunerile se obțin cu `scripts/facet_discovery.py` și le ratifică un om.
            raw_facets = {
                str(f.get("key")): f
                for f in ((business.settings or {}).get("domain_pack") or {}).get("facets") or []
                if isinstance(f, dict) and f.get("key")
            }
            name_specs = {
                key: pats
                for key, spec in raw_facets.items()
                if spec.get("source") == "name" and (pats := _value_patterns(spec))
            }
            claim_specs = {
                key: _claim_patterns(spec)
                for key, spec in raw_facets.items()
                if spec.get("claim_affirms")
            }
            undeclared = sorted(
                key
                for key in facet_values
                if key not in name_specs and key not in claim_specs and not facet_values[key]
            )
            skin_vals = facet_values.get("skin_type", set())
            concern_vals = facet_values.get("concerns", set())
            # NX-268: stemurile și excluderile sunt DATE, nu cod. Absente → potrivirea rămâne pe
            # fraze (cu toleranță de flexiune), adică exact comportamentul conservator.
            raw_pack = (business.settings or {}).get("domain_pack") or {}
            stems_cfg = raw_pack.get("concern_stems") or {}
            stems = {
                k: v.get("stems") or []
                for k, v in stems_cfg.items()
                if isinstance(k, str) and isinstance(v, dict)
            }
            excludes = {
                k: v.get("excludes") or []
                for k, v in stems_cfg.items()
                if isinstance(k, str) and isinstance(v, dict)
            }
            matchers = build_matchers(
                concern_map, stems=stems, excludes=excludes, normalize=normalize
            )
            n_stems = sum(len(m.stems) for m in matchers.values())
            n_excl = sum(len(m.excludes) for m in matchers.values())
            print(
                f"pachet: {len(concern_map)} fraze · skin_type {len(skin_vals)} valori · "
                f"concerns {len(concern_vals)} valori · {n_stems} stemuri · {n_excl} excluderi"
            )
            if undeclared:
                print(
                    f"fațete DECLARATE dar fără valori (nu se derivă): {', '.join(undeclared)}\n"
                    "  propuneri: python scripts/facet_discovery.py --business <uuid>"
                )

            products = await conn.fetch(
                """select p.id::text as id, p.name, coalesce(p.description,'') as description,
                          coalesce(cat.slug,'') as cat_slug, coalesce(cat.name,'') as cat_name,
                          p.attributes
                     from products p
                     left join categories cat on cat.id = p.primary_category_id
                    where p.business_id = $1 and p.status = 'active'""",
                args.business,
            )
            sections = await conn.fetch(
                """select product_id::text as id, kind, coalesce(body,'') as body
                     from product_sections where business_id = $1""",
                args.business,
            )
            by_product: dict[str, dict[str, list[str]]] = collections.defaultdict(
                lambda: collections.defaultdict(list)
            )
            for s in sections:
                by_product[s["id"]][s["kind"]].append(s["body"])

            tallies: dict[str, FacetTally] = collections.defaultdict(FacetTally)
            per_product: dict[str, dict] = {}
            no_need: list[dict] = []
            by_cat_missing: collections.Counter = collections.Counter()
            by_cat_total: collections.Counter = collections.Counter()
            vetoed_total: collections.Counter = collections.Counter()
            stem_only_products: collections.Counter = collections.Counter()
            # Ingredientele cer DOUĂ treceri: vocabularul canonic e cel mai frecvent din CATALOG,
            # deci nu se poate decide privind un singur produs. Prima trecere numără, a doua
            # atribuie. Fără asta, plafonul ar fi arbitrar per produs în loc de global.
            ingredient_heads: dict[str, list[str]] = {}
            ingredient_vocab = IngredientVocabulary(limit=args.ingredient_vocab)
            # Cuvintele goale ale locale-i taie capetele TRUNCHIATE de separator („extract de").
            function_words = stopwords(business.default_locale)

            for p in products:
                pid = p["id"]
                secs = by_product.get(pid, {})
                name_norm = normalize(p["name"])
                positive_parts: list[tuple[str, str]] = [("name", p["name"])]
                for kind in POSITIVE_SECTIONS:
                    for body in secs.get(kind, []):
                        positive_parts.append((kind, body))
                formula_parts = [(k, b) for k in FORMULA_SECTIONS for b in secs.get(k, [])] + [
                    ("description", p["description"])
                ]

                derived: dict[str, dict] = {}

                # --- nevoi + tip de ten: aceeași potrivire, fațete diferite -----------------
                # Regula de potrivire e în `src/catalog/derivation.py` (pură, testabilă fără DB):
                # frază cu tokeni pe PREFIX, în ordine, cu excluderi din pachet. Vezi acolo de ce
                # nu sunt stemuri dintr-un singur cuvânt.
                key_sources: dict[str, set[str]] = collections.defaultdict(set)
                key_phrases: dict[str, set[str]] = collections.defaultdict(set)
                key_stem_only: set[str] = set()
                for src, body in positive_parts:
                    for key, hit in match_keys(tokens(normalize(body)), matchers).items():
                        key_sources[key].add(src)
                        key_phrases[key].update(hit.evidence)
                        vetoed_total[key] += hit.vetoed
                        if hit.stem_only:
                            key_stem_only.add(key)
                        else:
                            key_stem_only.discard(key)

                concerns = sorted(k for k in key_sources if k in concern_vals)
                skin = sorted(k for k in key_sources if k in skin_vals)
                stem_only_products.update(k for k in key_sources if k in key_stem_only)
                if concerns:
                    derived["concerns"] = {
                        "values": concerns,
                        "rule_id": RULE_NEEDS,
                        "derived_from": sorted({s for k in concerns for s in key_sources[k]}),
                        "evidence": {k: sorted(key_phrases[k])[:3] for k in concerns},
                    }
                if skin:
                    derived["skin_type"] = {
                        "values": skin,
                        "rule_id": RULE_NEEDS,
                        "derived_from": sorted({s for k in skin for s in key_sources[k]}),
                        "evidence": {k: sorted(key_phrases[k])[:3] for k in skin},
                    }

                # --- ingrediente: linia e o propoziție, deci fațeta e CAPUL ei ---------------
                # Prima versiune scria linia întreagă: 99,1% acoperire, 10.392 de valori
                # distincte. Ca text de căutare, excelent; ca fațetă, inutilizabil — nimeni nu
                # poate filtra pe „ulei bogat în acizi grași mononesaturați ce reface lipidele".
                heads = []
                for body in secs.get("key_ingredients", []):
                    for line in _clean_lines(body):
                        if head := ingredient_head(line, function_words=function_words):
                            heads.append(head)
                if heads:
                    ingredient_heads[pid] = sorted(dict.fromkeys(heads))
                    for head in set(heads):
                        ingredient_vocab.observe(head)

                # --- SPF: cifră, deci se citește o dată, din nume, iar altundeva doar dacă
                # numele tace. Un „SPF 30" pomenit în proza unei rutine e despre alt produs.
                spf_m = SPF_RE.search(name_norm)
                spf_src = "name"
                if not spf_m:
                    spf_m = SPF_RE.search(normalize(p["description"]))
                    spf_src = "description"
                if spf_m:
                    derived["spf"] = {
                        "values": [spf_m.group(1)],
                        "rule_id": "regex.spf.v1",
                        "derived_from": [spf_src],
                    }

                # --- fațete de PROMISIUNE, din pachet (ex. „fără parfum") -------------------
                # Afirmativ explicit ȘI nicio contrazicere în același text. Frazele sunt ale
                # pachetului; fără ele fațeta nu se derivă, se raportează ca nedeclarată.
                formula_norm = " ".join(normalize(b) for _, b in formula_parts)
                for facet_key, (affirms, denies) in claim_specs.items():
                    if affirms is None:
                        continue
                    if affirms.search(formula_norm) and not (
                        denies is not None and denies.search(formula_norm)
                    ):
                        derived[facet_key] = {
                            "values": ["true"],
                            "rule_id": f"pack.claim.{facet_key}.strict.v1",
                            "derived_from": sorted({src for src, _ in formula_parts}),
                        }

                # --- fațete de FORMĂ, citite din NUME ---------------------------------------
                # Din nume, nu din proză: în proză „gel" apare și în „se aplică peste gelul de
                # curățare", care descrie ALT produs. Numele e forma produsului, o dată, fără
                # context de rutină. Valorile vin din pachet (vezi `_value_patterns`).
                for facet_key, value_pats in name_specs.items():
                    matched = sorted(
                        v
                        for v, pats in value_pats.items()
                        if any(x.search(name_norm) for x in pats)
                    )
                    if matched:
                        derived[facet_key] = {
                            "values": matched,
                            "rule_id": f"pack.name_values.{facet_key}.v1",
                            "derived_from": ["name"],
                        }

                per_product[pid] = derived
                by_cat_total[p["cat_name"] or "(fără categorie)"] += 1
                if not concerns and not skin:
                    by_cat_missing[p["cat_name"] or "(fără categorie)"] += 1
                    no_need.append(
                        {
                            "id": pid,
                            "name": p["name"][:110],
                            "category": p["cat_name"],
                            "has_sections": sorted(secs.keys()),
                        }
                    )

            # --- a doua trecere: ingredientele, cu vocabularul canonic acum cunoscut -----------
            # Ce nu intră în vocabular NU se aruncă: rămâne text în secțiune (deci în documentul de
            # căutare), doar că nu devine valoare de fațetă. Diferența e că o fațetă cu zece mii de
            # valori nu poate fi filtrată de nimeni, iar textul poate fi căutat de toată lumea.
            canonical_ingredients = ingredient_vocab.canonical()
            dropped_as_text = 0
            for pid, heads in ingredient_heads.items():
                keep = [h for h in heads if h in canonical_ingredients]
                dropped_as_text += len(heads) - len(keep)
                if keep:
                    per_product[pid]["key_ingredients"] = {
                        "values": keep[:12],
                        "rule_id": RULE_INGREDIENTS,
                        "derived_from": ["key_ingredients"],
                    }

            for pid, derived in per_product.items():
                for facet, info in derived.items():
                    t = tallies[facet]
                    t.products.add(pid)
                    t.values.update(info["values"])
                    t.sources.update(info["derived_from"])

            total = len(products)
            print(f"\nproduse active: {total}\n")
            print(f"{'fatetă':18}{'produse':>9}{'acoperire':>11}  valori distincte / top")
            for facet in (
                "concerns",
                "skin_type",
                "key_ingredients",
                "spf",
                "fragrance_free",
                "texture",
            ):
                t = tallies.get(facet, FacetTally())
                n = len(t.products)
                top = ", ".join(f"{v}:{c}" for v, c in t.values.most_common(6))
                cov = n / total if total else 0
                print(f"{facet:18}{n:>9}{cov:>10.1%}  {len(t.values)} / {top}")

            print(
                f"\ningrediente: {len(canonical_ingredients)} valori canonice (plafon "
                f"{args.ingredient_vocab}); {dropped_as_text} capete rămase TEXT, nu fațetă"
            )
            if n_stems:
                print(
                    f"stemuri: {sum(stem_only_products.values())} produse au intrat DOAR prin stem "
                    "(setul care trebuie auditat cu prioritate)"
                )
            if vetoed_total:
                print(f"excluderi active: {dict(vetoed_total.most_common(8))}")

            share = len(no_need) / total if total else 0
            print(f"\nfără NICIO nevoie și fără tip de ten: {len(no_need)} ({share:.1%})")
            print("\nunde se concentrează golul (top categorii):")
            print(f"  {'categorie':45}{'fără':>7}{'total':>7}{'rată':>8}")
            for cat, miss in by_cat_missing.most_common(10):
                tot = by_cat_total[cat]
                print(f"  {cat[:44]:45}{miss:>7}{tot:>7}{miss / tot:>8.0%}")

            if args.sample:
                print("\n--- exemple derivate (cu sursa) ---")
                shown = 0
                for pid, derived in per_product.items():
                    if not derived.get("concerns"):
                        continue
                    name = next(p["name"] for p in products if p["id"] == pid)
                    print(f"\n{name[:100]}")
                    for facet, info in derived.items():
                        print(
                            f"   {facet:16} {info['values']}  ← {info['rule_id']} "
                            f"[{', '.join(info['derived_from'])}]"
                        )
                        for key, phrases in (info.get("evidence") or {}).items():
                            print(f"      {key:18} ← {phrases}")
                    shown += 1
                    if shown >= args.sample:
                        break
                print("\n--- exemple FĂRĂ nicio nevoie derivată ---")
                for row in no_need[: min(8, args.sample)]:
                    print(f"  {row['name']}  · {row['category']} · secțiuni: {row['has_sections']}")

            out = pathlib.Path(
                args.out or ROOT / "reports" / f"derived-attributes-{args.business[:8]}.json"
            )
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(
                json.dumps(
                    {
                        "business_id": args.business,
                        "products_active": total,
                        "facets": {
                            f: {
                                "products": len(t.products),
                                "coverage": round(len(t.products) / total, 4) if total else 0,
                                "distinct_values": len(t.values),
                                "top_values": t.values.most_common(25),
                                "sources": t.sources.most_common(),
                            }
                            for f, t in sorted(tallies.items())
                        },
                        "without_any_need": {
                            "count": len(no_need),
                            "rate": round(len(no_need) / total, 4) if total else 0,
                            "by_category": by_cat_missing.most_common(),
                            "examples": no_need[:60],
                        },
                        "rules": {
                            "needs": RULE_NEEDS,
                            "key_ingredients": RULE_INGREDIENTS,
                            "spf": RULE_SPF,
                        },
                        "ingredients": {
                            "canonical": len(canonical_ingredients),
                            "limit": args.ingredient_vocab,
                            "left_as_text": dropped_as_text,
                        },
                        "stems": {
                            "declared": n_stems,
                            "excludes": n_excl,
                            "products_via_stem_only": dict(stem_only_products),
                            "vetoed": dict(vetoed_total),
                        },
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
            print(f"\nraport: {out}")

            if args.propose_stems:
                _propose_stems(products, by_product, concern_map, total)

            if not args.apply:
                print("\n(dry-run — nu s-a scris nimic în catalog; adaugă --apply)")
                return 0

            # --- scrierea -------------------------------------------------------------------
            # Fațetele proiectate în `attributes` sunt EXACT cele derivate acum. Cheile lor se
            # șterg înainte de scriere, ca o valoare rămasă dintr-o rulare veche să nu treacă
            # peste o re-derivare care n-o mai produce: UNKNOWN ar rămâne o valoare (anti-D7).
            facet_keys = sorted({f for d in per_product.values() for f in d})
            rows = [(pid, d) for pid, d in per_product.items() if d]
            written = touched = 0
            all_skipped: list[str] = []
            for i in range(0, len(rows), BATCH):
                s, t, skipped = await _write_batch(
                    conn,
                    args.business,
                    business.default_locale or "ro",
                    rows[i : i + BATCH],
                    facet_keys,
                )
                written += s
                touched += t
                all_skipped.extend(skipped)
            print(
                f"\nscris: {written} semnale, {touched} produse cu `attributes` schimbat "
                f"({len(rows)} produse cu fapte derivate)"
            )
            if all_skipped:
                print(f"sărite (eroare la scriere, lotul a continuat): {len(all_skipped)}")
            print("rerulează comanda: a doua trecere trebuie să raporteze 0 și 0 (idempotență)")
            return 0
    finally:
        await close_pool()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
