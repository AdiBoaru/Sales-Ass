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

    python scripts/derive_product_attributes.py --business <uuid>            # dry-run + raport
    python scripts/derive_product_attributes.py --business <uuid> --sample 12 # + exemple citate
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


def _phrase_hits(text_norm: str, concern_map: dict[str, str]) -> dict[str, list[str]]:
    """Cheile canonice susținute de text → frazele care le-au susținut.

    Potrivirea e pe frază întreagă cu granițe de cuvânt: `re.escape` + `\\b`. Fără granițe, „ser"
    din `concern_map` ar prinde „observat", iar o fațetă construită pe substringuri e o fațetă care
    minte fără să dea eroare."""
    hits: dict[str, list[str]] = collections.defaultdict(list)
    for phrase, key in concern_map.items():
        p = normalize(phrase)
        if not p:
            continue
        if re.search(rf"(?<!\w){re.escape(p)}(?!\w)", text_norm):
            hits[key].append(phrase)
    return dict(hits)


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--business", required=True)
    ap.add_argument("--sample", type=int, default=0, help="câte produse să afișeze cu citate")
    ap.add_argument("--out", default=None, help="unde scrie raportul JSON")
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
            print(
                f"pachet: {len(concern_map)} fraze · skin_type {len(skin_vals)} valori · "
                f"concerns {len(concern_vals)} valori"
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
                key_sources: dict[str, set[str]] = collections.defaultdict(set)
                key_phrases: dict[str, set[str]] = collections.defaultdict(set)
                for src, body in positive_parts:
                    for key, phrases in _phrase_hits(normalize(body), concern_map).items():
                        key_sources[key].add(src)
                        key_phrases[key].update(phrases)

                concerns = sorted(k for k in key_sources if k in concern_vals)
                skin = sorted(k for k in key_sources if k in skin_vals)
                if concerns:
                    derived["concerns"] = {
                        "values": concerns,
                        "rule_id": "concern_map.phrase_match.v1",
                        "derived_from": sorted({s for k in concerns for s in key_sources[k]}),
                        "evidence": {k: sorted(key_phrases[k])[:3] for k in concerns},
                    }
                if skin:
                    derived["skin_type"] = {
                        "values": skin,
                        "rule_id": "concern_map.phrase_match.v1",
                        "derived_from": sorted({s for k in skin for s in key_sources[k]}),
                        "evidence": {k: sorted(key_phrases[k])[:3] for k in skin},
                    }

                # --- ingrediente: secțiunea E deja lista, nu se extrage din proză -----------
                ingredients = []
                for body in secs.get("key_ingredients", []):
                    ingredients.extend(_clean_lines(body))
                if ingredients:
                    derived["key_ingredients"] = {
                        "values": sorted({i.lower() for i in ingredients})[:12],
                        "rule_id": "section.key_ingredients.lines.v1",
                        "derived_from": ["key_ingredients"],
                    }

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

                for facet, info in derived.items():
                    t = tallies[facet]
                    t.products.add(pid)
                    t.values.update(info["values"])
                    t.sources.update(info["derived_from"])

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
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
            print(f"\nraport: {out}")
            print("\n(dry-run — nu s-a scris nimic în catalog)")
            return 0
    finally:
        await close_pool()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
