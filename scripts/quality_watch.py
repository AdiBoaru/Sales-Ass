"""NX-272 — cele cinci cifre care spun dacă recomandarea s-a degradat. Read-only.

Toate măsurătorile din Wave H sunt **de unică folosință**: rulează la sfârșitul unui card, produc o
cifră, apoi nimeni nu se mai uită. Iar calitatea nu se degradează cu zgomot — se degradează tăcut,
la un import de catalog, la o schimbare de model, la un flag aprins din greșeală.

Precedentele sunt toate în proiect: `concern_map` a trimis cinci săptămâni spre valori inexistente;
`search_tsv` conținea doar numele produsului fiindcă `ai_summary` era NULL pe toate rândurile; CI-ul
e verde pe flagurile stinse, iar profilul de flaguri din producție n-a fost rulat niciodată.

## Cele cinci cifre, alese fiindcă nu pot fi umflate

1. **Rata de zero rezultate** — cifra care a scos la iveală 13 din 18 fraze moarte. Zero rezultate
   nu e o opinie.
2. **Rata de ture surde** — ture în care mesajul n-a produs NICIUN semnal structurat (nicio nevoie,
   nicio constrângere, nicio referință) **și** n-a găsit niciun produs. Nu e „rata de eșec", e ceva
   mai precis: turele în care sistemul n-a înțeles ȘI n-a găsit. Aia arată unde doare.
3. **Acoperirea fațetelor** — o regresie aici înseamnă că un import a șters atribute.
4. **Prospețimea catalogului** — pe `synced_at`, nu pe `updated_at`. `updated_at` se mișcă la orice
   scriere, inclusiv la una făcută de noi; `synced_at` spune când am mai vorbit cu sursa.
5. **Precizia pe capul distribuției** — setul NX-265 rerulat. Singura care cere judecată umană, și
   singura care lipsește azi.

## Ce NU măsoară, și de ce

**Conversie, venit, rată de adăugare în coș.** Sunt metricile care contează și nu se pot calcula:
zero trafic. A pretinde că le măsurăm pe 40 de conversații ar fi mai rău decât a nu le măsura. În
ziua în care există trafic, intră prin infrastructura de canary care există deja (NX-249: cohorte,
non-inferioritate pe interval Wilson, hard stops), nu printr-un al doilea sistem.

## Patru verdicte, nu două

`INSUFFICIENT` („prea puține date") e distinct de `FAIL` („am măsurat și e prost") și de `UNKNOWN`
(„instrumentul e stricat"). Sub eșantionul minim declarat, raportul spune „nu știu", nu „e bine" —
aceeași formă ca la NX-238 și NX-246 felia 3, care au rezistat.

**Nu e o poartă de CI.** Un raport care blochează merge-ul pe o metrică zgomotoasă va fi ocolit în
două săptămâni, iar atunci nu mai ai nici poartă, nici raport.

    python scripts/quality_watch.py --business <uuid> --days 7
"""

from __future__ import annotations

import argparse
import asyncio
import json
import pathlib
import sys
from datetime import datetime, timedelta, timezone

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from src.catalog.freshness import is_static  # noqa: E402
from src.db.connection import close_pool, tenant_conn  # noqa: E402
from src.observability.slo import (  # noqa: E402
    VERDICT_FAIL,
    VERDICT_INSUFFICIENT,
    VERDICT_PASS,
    VERDICT_UNKNOWN,
)

# Eșantionul sub care nicio cifră nu primește un verdict. Nu e prudență: pe 12 ture, o rată de 25%
# și una de 8% sunt aceeași măsurătoare.
MIN_SAMPLES = 30

# Pragurile, DECLARATE aici, nu alese după ce se văd cifrele. Sunt praguri de ALARMĂ, nu de
# acceptanță: peste ele ceva s-a stricat, sub ele nu înseamnă că e bine.
THRESHOLDS: dict[str, float] = {
    "zero_results_rate": 0.10,
    "deaf_turn_rate": 0.15,
    "facet_coverage_drop": 0.05,  # scădere față de raportul precedent
    "catalog_staleness_days": 7.0,
}

_ZERO_RESULTS = """
select count(*)                                                     as turns,
       count(*) filter (where coalesce(jsonb_array_length(recommended), 0) = 0) as empty
  from conversation_traces
 where business_id = $1 and created_at >= $2 and created_at < $3
"""

# „Tur surd" = n-a înțeles ȘI n-a găsit. Cele două condiții împreună, nu separat: un tur care n-a
# găsit nimic dar a înțeles perfect („n-avem SPF 50 sub 50 lei") e un răspuns bun despre un catalog
# incomplet, nu un eșec de sistem.
_DEAF_TURNS = """
select count(*) as turns,
       count(*) filter (
           where coalesce(jsonb_array_length(recommended), 0) = 0
             and coalesce(diagnostics->'needs', '[]'::jsonb) = '[]'::jsonb
             and coalesce(diagnostics->'constraints', '[]'::jsonb) = '[]'::jsonb
             and coalesce(diagnostics->'references', '[]'::jsonb) = '[]'::jsonb
       ) as deaf
  from conversation_traces
 where business_id = $1 and created_at >= $2 and created_at < $3
"""

_FACET_COVERAGE = """
select count(*)                                                  as products,
       count(*) filter (where attributes ? 'concerns')           as concerns,
       count(*) filter (where attributes ? 'skin_type')          as skin_type,
       count(*) filter (where attributes ? 'shade')              as shade
  from products
 where business_id = $1 and status = 'active'
"""

# `synced_at`, nu `updated_at`: al doilea se mișcă la orice scriere, inclusiv la una făcută de noi
# (derivarea NX-268 ar face catalogul să pară proaspăt chiar dacă sursa tace de o lună).
_FRESHNESS = """
select max(synced_at) as newest,
       count(*) filter (where synced_at is null) as without_sync,
       count(*) as products
  from products
 where business_id = $1 and status = 'active'
"""

# Declarația de prospețime a tenantului. Citită de pe conexiunea tenant-scoped, ca tot restul
# raportului: e o proprietate a ACESTUI business, nu configurație de mediu.
_BUSINESS_SETTINGS = """
select settings from businesses where id = $1
"""


def _verdict(value: float | None, threshold: float, samples: int, *, higher_is_worse=True) -> str:
    """Verdictul unei cifre. Ordinea condițiilor E contractul: instrumentul stricat înaintea
    eșantionului mic, eșantionul mic înaintea judecății. Un `PASS` obținut pe date lipsă e mai
    periculos decât un `FAIL`."""
    if value is None:
        return VERDICT_UNKNOWN
    if samples < MIN_SAMPLES:
        return VERDICT_INSUFFICIENT
    bad = value > threshold if higher_is_worse else value < threshold
    return VERDICT_FAIL if bad else VERDICT_PASS


def declared_settings(raw: object) -> dict:
    """`businesses.settings` → dict, oricare ar fi forma în care vine de pe conexiune.

    Există fiindcă `settings` sosește ca ȘIR pe conexiunile fără codec `jsonb` înregistrat, iar
    `freshness.is_static` e pură și primește un `Mapping`. Fără pasul ăsta, raportul arunca
    `AttributeError` pe baza reală și trecea în teste, care exersează doar funcția pură — exact
    clasa de defect care a ținut joburile de derivare nerulabile: cod exersat într-un singur regim.

    Orice altceva decât un obiect JSON devine `{}`: o declarație malformată nu e o declarație, iar
    politica pentru necunoscut e cea conservatoare (judecăm vechimea)."""
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            return {}
    return raw if isinstance(raw, dict) else {}


def _row(name: str, value: float | None, threshold: float, samples: int, **kw) -> dict:
    return {
        "metric": name,
        "value": None if value is None else round(value, 4),
        "threshold": threshold,
        "samples": samples,
        "verdict": _verdict(value, threshold, samples, **kw),
    }


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--business", required=True)
    ap.add_argument("--days", type=int, default=7, help="fereastra de analiză, în zile")
    ap.add_argument(
        "--until",
        default=None,
        help=(
            "sfârșitul ferestrei, ISO (default: azi la 00:00 UTC). Explicit, ca aceeași fereastră "
            "să dea același rezultat: un raport care se schimbă între două rulări nu poate fi "
            "comparat cu el însuși."
        ),
    )
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    end = (
        datetime.fromisoformat(args.until).replace(tzinfo=timezone.utc)
        if args.until
        else datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    )
    start = end - timedelta(days=args.days)

    try:
        async with tenant_conn(args.business) as conn:
            has_traces = await conn.fetchval(
                "select to_regclass('public.conversation_traces') is not null"
            )
            metrics: list[dict] = []

            if has_traces:
                zero = dict(await conn.fetchrow(_ZERO_RESULTS, args.business, start, end) or {})
                turns = int(zero.get("turns") or 0)
                rate = (int(zero.get("empty") or 0) / turns) if turns else None
                metrics.append(
                    _row("zero_results_rate", rate, THRESHOLDS["zero_results_rate"], turns)
                )

                deaf = dict(await conn.fetchrow(_DEAF_TURNS, args.business, start, end) or {})
                dturns = int(deaf.get("turns") or 0)
                drate = (int(deaf.get("deaf") or 0) / dturns) if dturns else None
                metrics.append(_row("deaf_turn_rate", drate, THRESHOLDS["deaf_turn_rate"], dturns))
            else:
                # Migrarea 045 lipsește ⇒ instrumentul nu există. `UNKNOWN`, nu zero: „n-am măsurat"
                # și „am măsurat zero" sunt lucruri diferite, iar al doilea ar arăta ca o victorie.
                for name in ("zero_results_rate", "deaf_turn_rate"):
                    metrics.append(_row(name, None, THRESHOLDS[name], 0))

            cov = dict(await conn.fetchrow(_FACET_COVERAGE, args.business) or {})
            products = int(cov.get("products") or 0)
            for facet in ("concerns", "skin_type", "shade"):
                covered = int(cov.get(facet) or 0)
                share = (covered / products) if products else None
                metrics.append(
                    {
                        "metric": f"facet_coverage.{facet}",
                        "value": None if share is None else round(share, 4),
                        "threshold": None,  # se compară cu rulările ANTERIOARE, nu cu un prag
                        "samples": products,
                        "verdict": VERDICT_UNKNOWN if share is None else "MEASURED",
                    }
                )

            fresh = dict(await conn.fetchrow(_FRESHNESS, args.business) or {})
            newest = fresh.get("newest")
            age_days = (end - newest).total_seconds() / 86400 if newest else None
            # Cine judecă vechimea în timp e TENANTUL, nu o constantă a raportului. Un catalog
            # declarat `static_snapshot` e o fotografie importată o dată, fără proces care s-o
            # reîmprospăteze — deci n-ar exista niciodată un „proaspăt" în care să intre înapoi, iar
            # un prag i-ar da `FAIL` zilnic, tot mai tare, despre nimic. `freshness.is_static`
            # există exact pentru rapoarte („o scutire permanentă trebuie să fie VIZIBILĂ"), doar că
            # raportul ăsta n-o chema. Vechimea se RAPORTEAZĂ în continuare — a o ascunde ar fi
            # cealaltă greșeală, fiindcă un snapshot vechi de un an rămâne un fapt despre catalog.
            # `settings` vine ca ȘIR pe conexiunea asta (fără codec jsonb înregistrat), nu ca dict.
            # `is_static` e pură și primește un Mapping; fără decodare ar arunca `AttributeError`
            # exact în producție și niciodată în teste, fiindcă testele exersează funcția pură.
            settings_row = await conn.fetchrow(_BUSINESS_SETTINGS, args.business)
            raw = settings_row["settings"] if settings_row else None
            static = is_static(declared_settings(raw))
            if static:
                metrics.append(
                    {
                        "metric": "catalog_staleness_days",
                        "value": None if age_days is None else round(age_days, 4),
                        "threshold": None,
                        "samples": int(fresh.get("products") or 0),
                        "verdict": VERDICT_UNKNOWN if age_days is None else "MEASURED",
                        "note": "catalog declarat `static_snapshot` — vechimea nu se judecă",
                    }
                )
            else:
                metrics.append(
                    _row(
                        "catalog_staleness_days",
                        age_days,
                        THRESHOLDS["catalog_staleness_days"],
                        int(fresh.get("products") or 0),
                    )
                )

            # Cifra 5: setul NX-265. Nu se calculează aici — se citește raportul lui, dacă există.
            # Un raport de calitate care își inventează propriul set de evaluare ar fi al doilea
            # instrument de măsură, iar două instrumente care nu coincid nu măsoară nimic.
            goldset = ROOT / "reports" / f"goldset-{args.business[:8]}.json"
            metrics.append(
                {
                    "metric": "head_precision_top3",
                    "value": None,
                    "threshold": None,
                    "samples": 0,
                    "verdict": VERDICT_UNKNOWN,
                    "note": (
                        "setul NX-265 nu e adnotat: rulează `scripts/goldset_annotate.py`, apoi "
                        "`scripts/goldset_report.py --label baseline`"
                    )
                    if not goldset.exists()
                    else "citit din raportul NX-265",
                }
            )

        print(f"fereastra: {start.date()} → {end.date()} ({args.days} zile)")
        print(f"{'metrica':30}{'valoare':>10}{'prag':>10}{'n':>8}  verdict")
        for m in metrics:
            value = "—" if m["value"] is None else f"{m['value']:.3f}"
            threshold = "—" if m["threshold"] is None else f"{m['threshold']:.3f}"
            print(f"{m['metric']:30}{value:>10}{threshold:>10}{m['samples']:>8}  {m['verdict']}")

        out = pathlib.Path(args.out or ROOT / "reports" / f"quality-watch-{args.business[:8]}.json")
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(
            json.dumps(
                {
                    "business_id": args.business,
                    "window": {"from": start.isoformat(), "to": end.isoformat()},
                    "min_samples": MIN_SAMPLES,
                    "metrics": metrics,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        print(f"\nraport: {out}")
        # Cod 0 chiar pe `FAIL`: NU e o poartă de CI. Un raport care blochează merge-ul pe o
        # metrică zgomotoasă va fi ocolit în două săptămâni, iar atunci nu mai ai nici poartă,
        # nici raport.
        return 0
    finally:
        await close_pool()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
