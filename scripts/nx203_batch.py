"""NX-203 — etichetare în lot: exportă familii de judecat, importă judecățile înapoi.

Unealta interactivă (`nx203_label.py`) e făcută pentru un om cu o tastatură. Asta e pentru un
etichetator care citește text și scrie text — un model, sau un om care preferă un editor. Aceleași
date, același fișier de stare, aceeași semantică; se pot alterna.

**Autorul etichetei se ÎNREGISTREAZĂ.** `--labeler model` scrie `labelers[family_id] = "model"`, iar
`finalize` derivă `human_verified` din el. Un corpus etichetat de model nu poate deschide feliile
sigilate: gate-ul cumpără exact independența față de modelul măsurat, iar dacă autorul etichetei e
chiar el, corpusul își certifică propriul autor. Marcajul face asta vizibil în DATE, nu doar în
discuție — cine ia fișierul peste șase luni nu are de unde ști altfel.

Rubrica de relevanță e tipărită la fiecare export, ca judecata să nu derive între loturi:

    3  exact ce a cerut  — tipul corect ȘI rafinarea cerută (ingredient, nevoie, atribut)
    2  relevant          — tipul corect, răspuns plauzibil, dar fără rafinarea specifică
    1  marginal          — înrudit, dar alt subtip; sau ingredientul cerut pe alt tip de produs
    0  nerelevant        — altă categorie de produs
    x  INTERZIS          — încalcă o cerință explicită a cererii (cere și un motiv scris)

    python scripts/nx203_batch.py export --limit 10
    python scripts/nx203_batch.py export --limit 10 --skip 10
    python scripts/nx203_batch.py apply --labeler model --file batch.json
    python scripts/nx203_batch.py status
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

if sys.platform == "win32" and hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

DATA_DIR = ROOT / "tests" / "golden" / "nx203"
POOLS = DATA_DIR / "pools.json"
FAMILIES = DATA_DIR / "families_draft.json"
JUDGMENTS = DATA_DIR / "judgments.json"

RUBRIC = """3 exact (tip corect + rafinarea ceruta) · 2 relevant (tip corect, fara rafinare)
1 marginal (alt subtip, sau ingredientul pe alt tip) · 0 nerelevant (alta categorie)
x interzis (incalca o cerinta explicita)"""

#: Cât din numele de produs se arată. Numele SOLE au 191 de caractere în medie și poartă o coadă de
#: marketing; capul plus tipul canonic sunt tot ce trebuie ca să judeci potrivirea.
_NAME_CHARS = 76


def _load() -> tuple[dict, dict, dict]:
    pools = json.loads(POOLS.read_text(encoding="utf-8"))
    families = {
        f["family_id"]: f for f in json.loads(FAMILIES.read_text(encoding="utf-8"))["families"]
    }
    state = (
        json.loads(JUDGMENTS.read_text(encoding="utf-8"))
        if JUDGMENTS.exists()
        else {
            "business_id": pools["business_id"],
            "catalog_version": pools["catalog_version"],
            "started_at": datetime.now(UTC).isoformat(timespec="seconds"),
            "judgments": {},
            "family_notes": {},
            "rationales": {},
            "labelers": {},
        }
    )
    return pools, families, state


def _pending(pools: dict, state: dict) -> list[str]:
    """Familiile fără nicio judecată, în ordinea din pool (cele mai solide primele)."""
    done = state.get("judgments", {})
    return [fid for fid in pools["pools"] if fid not in done]


def cmd_export(args) -> int:
    pools, families, state = _load()
    pending = _pending(pools, state)[args.skip : args.skip + args.limit]
    if not pending:
        print("Nimic de exportat: toate familiile au judecăți.")
        return 0

    products = pools["products"]
    print(f"# NX-203 lot · {len(pending)} familii\n# {RUBRIC}\n")
    for fid in pending:
        fam = families[fid]
        cons = ", ".join(f"{c['facet']}={c['value']}" for c in fam["hard_constraints"]) or "—"
        unres = ", ".join(fam["unresolved_terms"]) or "—"
        print(
            f"## {fid}  [{fam['query_class']}]{'  ⚠FUZIUNE-SUSPECTĂ' if fam['needs_split'] else ''}"
        )
        for q in fam["queries"][:3]:
            print(f"   cerere: {q}")
        print(f"   tip={fam['product_type']} · constrângeri: {cons} · nerezolvat: {unres}")
        for i, entry in enumerate(pools["pools"][fid]):
            p = products.get(entry["product_id"], {})
            name = (p.get("name") or "?")[:_NAME_CHARS]
            ptype = p.get("product_type") or "?"
            brand = p.get("brand") or "?"
            print(f"   {i:2}. {name}")
            print(f"       tip={ptype} · {brand} · {p.get('availability') or '?'}")
        print()
    return 0


def cmd_apply(args) -> int:
    """Aplică un lot: `{family_id: {"j": "3210x...", "note": {...}}}` sau `{fid: {pid: rel}}`.

    Forma scurtă (un șir de note, în ordinea din pool) e cea folosită la etichetarea în lot: e
    compactă și, mai important, e POZIȚIONALĂ — nu poți greși un id de produs, fiindcă nu-l scrii.
    Lungimea șirului trebuie să fie exact cât pool-ul; altfel lotul se respinge întreg, nu parțial.
    """
    pools, _families, state = _load()
    batch = json.loads(Path(args.file).read_text(encoding="utf-8"))
    applied = 0
    errors: list[str] = []

    for fid, payload in batch.items():
        pool = pools["pools"].get(fid)
        if pool is None:
            errors.append(f"{fid}: familie inexistentă în pool")
            continue
        if isinstance(payload, dict) and "j" in payload:
            marks = list(str(payload["j"]).replace(" ", ""))
            if len(marks) != len(pool):
                errors.append(f"{fid}: {len(marks)} note pentru {len(pool)} candidați")
                continue
            judged = {}
            for entry, mark in zip(pool, marks, strict=True):
                if mark not in "0123x":
                    errors.append(f"{fid}: nota '{mark}' nu e din rubrică")
                    break
                judged[entry["product_id"]] = "forbidden" if mark == "x" else int(mark)
            else:
                reasons = payload.get("why") or {}
                for pos, why in reasons.items():
                    pid = pool[int(pos)]["product_id"]
                    state.setdefault("rationales", {}).setdefault(fid, {})[pid] = why
                missing = [
                    pid
                    for pid, v in judged.items()
                    if v == "forbidden" and pid not in state.get("rationales", {}).get(fid, {})
                ]
                if missing:
                    errors.append(f"{fid}: interdicție fără motiv scris ({len(missing)})")
                    continue
                state["judgments"][fid] = judged
                state.setdefault("labelers", {})[fid] = args.labeler
                if note := payload.get("family_note"):
                    state.setdefault("family_notes", {})[fid] = {
                        "action": note["action"],
                        "note": note.get("note", ""),
                        "at": datetime.now(UTC).isoformat(timespec="seconds"),
                    }
                applied += 1
        else:
            errors.append(f"{fid}: format necunoscut (aștept {{'j': '3210…'}})")

    if errors:
        print("RESPINSE:")
        for e in errors:
            print("  -", e)
    if applied:
        state["updated_at"] = datetime.now(UTC).isoformat(timespec="seconds")
        tmp = JUDGMENTS.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(JUDGMENTS)
    total = sum(len(v) for v in state["judgments"].values())
    print(f"aplicate: {applied} familii · total judecăți: {total}")
    return 1 if errors else 0


def cmd_status(_args) -> int:
    pools, _families, state = _load()
    done = state.get("judgments", {})
    labelers = state.get("labelers", {})
    total_c = sum(len(p) for p in pools["pools"].values())
    done_c = sum(len(v) for v in done.values())
    by_labeler: dict[str, int] = {}
    for fid in done:
        by_labeler[labelers.get(fid, "?")] = by_labeler.get(labelers.get(fid, "?"), 0) + 1
    print(f"familii: {len(done)}/{len(pools['pools'])} · judecăți: {done_c}/{total_c}")
    print(f"autor etichetelor: {by_labeler or '—'}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = ap.add_subparsers(dest="cmd", required=True)
    e = sub.add_parser("export")
    e.add_argument("--limit", type=int, default=10)
    e.add_argument("--skip", type=int, default=0)
    e.set_defaults(fn=cmd_export)
    a = sub.add_parser("apply")
    a.add_argument("--file", required=True)
    a.add_argument("--labeler", required=True, choices=["human", "model"])
    a.set_defaults(fn=cmd_apply)
    s = sub.add_parser("status")
    s.set_defaults(fn=cmd_status)
    args = ap.parse_args()
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())
