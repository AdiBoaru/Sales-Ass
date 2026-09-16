"""Ce pas cedează primul când rutina nu încape — derivat din conținutul clientului (NX-292).

De ce există. `routine_steps.families` declară ORDINEA de aplicare (curățare → tonifiere → …), care
e o proprietate fizică: tonicul se pune după spălare. Nu declară însă nimic despre ESENȚIALITATE,
iar compunerea avea nevoie de asta: cu un buget care nu acoperă toți pașii, cineva trebuie să decidă
care pas cedează. Fără ordinea de sacrificiu, `routine_plan` cerea toți pașii declarați și, când nu
încăpeau, răspundea „nu se poate" cu o cifră care era artefactul insistenței pe lungimea maximă.
Măsurat pe `sole-ro`: „rutină pentru ten uscat sub 200 lei" primea „minimul e 375", deși patru pași
costă 165.

**Numărul de pași nu se declară nicăieri, nici aici.** Scriptul nu emite o lungime și nici un set
„de bază": emite o ORDINE. Lungimea o decide bugetul turului, adică se schimbă de la client la
client — exact ce cere produsul.

## De unde vine ordinea

Din `aura.routine_integration`, secțiunea în care sursa descrie, per produs, o secvență numerotată
explicită («Ordinea tipică: 1. … 2. … 3. …»). Pe catalogul SOLE există pe 2.533 de produse și
FIECARE conține o secvență. Frecvența cu care un pas apare în rutinele descrise de magazinul însuși
e o afirmație a lui despre cât de indispensabil e pasul, deci un denominator pe care nu l-am
inventat noi. Alternativele erau mai proaste: adâncimea de inventar ar confunda merchandising-ul cu
necesitatea (588 de tratamente vs 99 de protecții solare nu înseamnă că serul bate SPF-ul), iar o
listă scrisă de mână ar fi opinia celui care a scris-o.

## Cohorta de TIMP nu e un rafinament, e condiția corectitudinii

Pe toate secvențele la un loc, protecția solară apare în 44,2% — sub tonifiere (51,2%). O ordine
derivată așa ar tăia SPF-ul înaintea tonicului, adică sfat prost servit cu cifre reale. Separat pe
cohorte, aceleași date spun altce: **97,3% în secvențele care vorbesc doar despre dimineață, 10,7%
în cele doar despre seară**. Protecția nu e opțională, e a dimineții. Deci scriptul derivă o ordine
PER MOMENT al zilei și, separat, momentul în care pasul e relevant.

## Ce e vocabular și ce e cod (P9)

Recunoașterea unui pas în proză cere tulpini românești („curat", „toner", „spf"). Alea sunt
vocabular de vertical, deci se citesc din `routine_steps.step_stems` (pachetul tenantului), nu stau
aici. Fără ele scriptul refuză să ghicească: o potrivire pe numele tipurilor de produs a fost
încercată și MĂSURATĂ ca părtinitoare: textul spune „serul", pachetul are „ser de fata", deci
tratamentul ieșea 18,3% în loc de 72,5%.

Citește DOAR (`SELECT`, tenant-scoped). Zero OpenAI, zero scriere fără `--apply`.

    python scripts/derive_routine_priority.py --business <uuid>
    python scripts/derive_routine_priority.py --business <uuid> --json reports/routine/priority.json
"""

from __future__ import annotations

import argparse
import asyncio
import collections
import json
import pathlib
import re
import sys
from typing import Any

ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    sys.stdout.reconfigure(encoding="utf-8")

from src.db.connection import tenant_conn  # noqa: E402
from src.db.queries.businesses import load_business  # noqa: E402
from src.domain.normalize import normalize  # noqa: E402
from src.domain.routine_steps import SEP  # noqa: E402

#: Itemii unei secvențe numerotate: „1. text" până la următorul „N." sau la final. Ancorat ca
#: numărul să NU fie o zecimală dintr-un preț sau o cantitate („15–20 minute", „SPF 50").
ITEM_RE = re.compile(r"(?<!\d)(\d{1,2})[.)]\s*(.+?)(?=(?<!\d)\d{1,2}[.)]\s|$)", re.S)

#: O secvență are cel puțin doi pași numerotați. Unul singur e o instrucțiune de folosire, nu o
#: rutină, iar numărarea lui ar umfla denominatorul cu produse care nu descriu nicio secvență.
MIN_ITEMS = 2

#: Sub atâtea secvențe, cohorta nu primește ordine proprie: cu 20 de exemple, diferența dintre 60%
#: și 45% e zgomot, iar o ordine derivată din zgomot ar muta pași reali. Cohorta insuficientă
#: MOȘTENEȘTE ordinea globală și o declară ca atare — `INSUFFICIENT` ≠ un rezultat (ca la NX-246).
MIN_SEQUENCES_PER_COHORT = 50

#: Marcaje de moment al zilei. Vocabular de LIMBĂ, nu de vertical (o rutină de service auto n-are
#: dimineață), deci pragul de aplicare e prezența lor în pachet: `time_markers` absent ⇒ nu se
#: derivă cohorte, se emite doar ordinea globală.
_DEFAULT_TIME_KEY = "time_markers"

#: Peste atâta prezență într-o cohortă și sub atâta în cealaltă, pasul e declarat AL cohortei, nu
#: opțional. Nu e un prag de calitate inventat: e testul care separă „apare rar" de „apare rar
#: fiindcă nu e momentul lui", iar pe date reale diferența e 97,3% vs 10,7%, nu una marginală.
TIME_BOUND_HIGH = 0.80
TIME_BOUND_LOW = 0.25


def _stems(pack: object) -> dict[str, tuple[str, ...]]:
    spec = getattr(pack, "routine_steps", None)
    raw = getattr(spec, "step_stems", None) or {}
    return {str(k): tuple(normalize(s) for s in v) for k, v in raw.items() if v}


def _time_markers(pack: object) -> dict[str, tuple[str, ...]]:
    spec = getattr(pack, "routine_steps", None)
    raw = getattr(spec, _DEFAULT_TIME_KEY, None) or {}
    return {str(k): tuple(normalize(s) for s in v) for k, v in raw.items() if v}


def steps_in(text: str, stems: dict[str, tuple[str, ...]]) -> set[str]:
    """Pașii numiți în bucata de text dată. Potrivire pe tulpină, nu pe cuvânt întreg: proza
    flexionează („curăță", „curățare", „curățat") și româna o face des."""
    t = normalize(text)
    return {step for step, terms in stems.items() if any(x in t for x in terms)}


def cohort_of(text: str, markers: dict[str, tuple[str, ...]]) -> str:
    """Momentul zilei despre care vorbește secvența. `both` când le numește pe amândouă,
    `unspecified` când pe niciuna — amândouă sunt răspunsuri, nu lipsuri."""
    t = normalize(text)
    present = {name for name, terms in markers.items() if any(x in t for x in terms)}
    if len(present) == 1:
        return next(iter(present))
    return "both" if present else "unspecified"


def derive(
    rows: list[dict[str, Any]],
    *,
    families: dict[str, tuple[str, ...]],
    stems: dict[str, tuple[str, ...]],
    markers: dict[str, tuple[str, ...]],
) -> dict[str, Any]:
    """Frecvențele → ordine per familie (și per cohortă de timp). PUR: testabil fără DB."""
    seqs: dict[str, list[tuple[str, set[str]]]] = collections.defaultdict(list)
    skipped = 0
    for row in rows:
        family = str(row["step"]).partition(SEP)[0]
        if family not in families:
            continue
        body = row["body"] or ""
        items = [m.group(2).strip() for m in ITEM_RE.finditer(body)]
        if len(items) < MIN_ITEMS:
            skipped += 1
            continue
        named: set[str] = set()
        for item in items:
            named |= steps_in(item, stems)
        seqs[family].append((cohort_of(body, markers), named & set(families[family])))

    out: dict[str, Any] = {"skipped_without_sequence": skipped, "families": {}}
    for family, steps in families.items():
        observed = seqs.get(family) or []
        n = len(observed)
        entry: dict[str, Any] = {"n_sequences": n, "steps": {}, "cohorts": {}}
        if not n:
            entry["verdict"] = "INSUFFICIENT"
            out["families"][family] = entry
            continue

        def share(step: str, pool: list[tuple[str, set[str]]]) -> float:
            return (sum(1 for _, named in pool if step in named) / len(pool)) if pool else 0.0

        for step in steps:
            entry["steps"][step] = round(share(step, observed), 4)

        by_cohort = collections.defaultdict(list)
        for cohort, named in observed:
            by_cohort[cohort].append((cohort, named))

        for cohort, pool in sorted(by_cohort.items()):
            shares = {step: round(share(step, pool), 4) for step in steps}
            entry["cohorts"][cohort] = {
                "n": len(pool),
                "shares": shares,
                "verdict": ("OK" if len(pool) >= MIN_SEQUENCES_PER_COHORT else "INSUFFICIENT"),
                "priority": _order(shares) if len(pool) >= MIN_SEQUENCES_PER_COHORT else None,
            }

        entry["global_priority"] = _order(entry["steps"])
        entry["priority"] = _priority_by_moment(entry, markers)
        entry["step_time"] = _time_binding(entry["cohorts"], steps, markers)
        entry["verdict"] = "OK" if n >= MIN_SEQUENCES_PER_COHORT else "INSUFFICIENT"
        out["families"][family] = entry
    return out


def _priority_by_moment(
    entry: dict[str, Any], markers: dict[str, tuple[str, ...]]
) -> dict[str, Any]:
    """`moment → ordine`, în forma pe care o consumă pachetul.

    `default` NU e ordinea globală și nici cea a secvențelor fără moment declarat. Amândouă ar fi
    greșite, și greșit exact pe pasul care contează: pe medie protecția solară iese a cincea, iar
    în secvențele fără moment apare în 4,2%, deci ar cădea PRIMA la un buget strâns. Clientul care
    cere „o rutină" fără să spună când vrea o rutină de ZI, iar aia e cohorta `both` — secvențele
    în care sursa descrie și dimineața și seara. Fără ea (eșantion insuficient), se cade pe global.
    """
    cohorts = entry["cohorts"]
    day = cohorts.get("both") or {}
    default = day.get("priority") if day.get("verdict") == "OK" else None
    out: dict[str, Any] = {"default": default or entry["global_priority"]}
    out["_default_from"] = "cohorta `both`" if default else "ordinea globală (both insuficient)"
    for moment in markers:
        c = cohorts.get(moment) or {}
        if c.get("priority"):
            out[moment] = c["priority"]
    return out


def _order(shares: dict[str, float]) -> list[str]:
    """Ordinea de sacrificiu: cel mai frecvent primul, cedează ultimul. Tie-break pe NUME, ca două
    rulări pe același catalog să dea aceiași octeți (fără el, ordinea ar depinde de dict)."""
    return [s for s, _ in sorted(shares.items(), key=lambda kv: (-kv[1], kv[0]))]


def _time_binding(
    cohorts: dict[str, Any], steps: tuple[str, ...], markers: dict[str, tuple[str, ...]]
) -> dict[str, str]:
    """Pasul care apare masiv într-o cohortă de timp și aproape deloc în cealaltă e AL ei.

    Distincția salvează corectitudinea: pe medie, protecția solară pare mai puțin esențială decât
    tonicul, iar o ordine derivată din medie ar tăia SPF-ul primul. Un pas nelegat de timp rămâne
    `both`, care e și default-ul prudent."""
    names = [c for c in cohorts if c in markers]
    binding: dict[str, str] = {}
    for step in steps:
        usable = {
            c: cohorts[c]["shares"].get(step, 0.0) for c in names if cohorts[c]["verdict"] == "OK"
        }
        if len(usable) < 2:
            binding[step] = "both"
            continue
        high = max(usable, key=lambda c: usable[c])
        low = min(usable, key=lambda c: usable[c])
        if usable[high] >= TIME_BOUND_HIGH and usable[low] <= TIME_BOUND_LOW:
            binding[step] = high
        else:
            binding[step] = "both"
    return binding


async def run(business_id: str, out_json: pathlib.Path | None) -> int:
    async with tenant_conn(business_id) as conn:
        biz = await load_business(conn, business_id)
        if biz is None:
            print(f"EROARE: business {business_id} inexistent", file=sys.stderr)
            return 2
        pack = getattr(biz, "domain_pack", None)
        spec = getattr(pack, "routine_steps", None)
        families = dict(getattr(spec, "families", {}) or {})
        if not families:
            print("EROARE: tenantul n-are `domain_pack.routine_steps.families`", file=sys.stderr)
            return 2
        stems = _stems(pack)
        missing = [s for steps in families.values() for s in steps if s not in stems]
        if missing:
            print(
                "EROARE: lipsesc tulpinile pentru pașii "
                f"{sorted(set(missing))} — declară-le în `routine_steps.step_stems`.\n"
                "Nu le ghicesc: o potrivire pe numele tipurilor de produs a fost măsurată ca "
                "părtinitoare (tratamentul ieșea 18% în loc de 72%).",
                file=sys.stderr,
            )
            return 2
        markers = _time_markers(pack)

        rows = await conn.fetch(
            """
            select p.attributes->>'routine_step' as step, s.body
            from product_sections s
            join products p on p.id = s.product_id and p.business_id = s.business_id
            where s.business_id = $1 and s.source = 'aura' and s.kind = 'routine_integration'
              and p.status = 'active' and p.attributes->>'routine_step' is not null
            order by p.id
            """,
            business_id,
        )

    report = derive([dict(r) for r in rows], families=families, stems=stems, markers=markers)

    print(f"business: {business_id}")
    print(f"secvențe fără listă numerotată (ignorate): {report['skipped_without_sequence']}\n")
    for family, entry in report["families"].items():
        print(f"── {family}: {entry['n_sequences']} secvențe · {entry['verdict']} ──")
        if not entry["n_sequences"]:
            print("   fără conținut de rutină, pasul nu primește ordine\n")
            continue
        print("   pas            global   " + "".join(f"{c:>14s}" for c in entry["cohorts"]))
        for step in _order(entry["steps"]):
            cells = "".join(
                f"{100 * entry['cohorts'][c]['shares'].get(step, 0.0):>13.1f}%"
                for c in entry["cohorts"]
            )
            print(f"   {step:14s} {100 * entry['steps'][step]:5.1f}%   {cells}")
        prio = entry["priority"]
        print(f"   ordine globală (informativă): {entry['global_priority']}")
        print(f"   → priority.default [{prio['_default_from']}]: {prio['default']}")
        for moment in sorted(k for k in prio if k not in ("default", "_default_from")):
            print(f"   → priority.{moment}: {prio[moment]}")
        print(f"   step_time: {entry['step_time']}")
        for cohort, c in entry["cohorts"].items():
            if not c["priority"]:
                print(f"   «{cohort}» n={c['n']} → INSUFFICIENT, nu produce ordine")
        print()

    if out_json:
        out_json.parent.mkdir(parents=True, exist_ok=True)
        out_json.write_text(
            json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8"
        )
        print(f"raport scris: {out_json}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--business", required=True)
    ap.add_argument("--json", type=pathlib.Path, default=None)
    args = ap.parse_args()
    return asyncio.run(run(args.business, args.json))


if __name__ == "__main__":
    raise SystemExit(main())
