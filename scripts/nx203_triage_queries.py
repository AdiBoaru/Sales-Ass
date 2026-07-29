"""NX-203 — TRIAJ de eligibilitate: produce candidati si MOTIVE, nu verdicte.

Distinctia care structureaza scriptul (audit Codex, PR #251):
  · ELIGIBILITATE (sta singura? e retrieval? nu e safety?) — proprietate a TEXTULUI, triabila
    mecanic dupa criterii scrise;
  · RELEVANTA (ce produse se potrivesc) — judecata despre potrivire, cere OM.
Dar cu o limita explicita: triajul de aici NU e verdictul final de eligibilitate. Produce
dispozitii propuse cu reason code; omul confirma eligibilitatea celor 190 care intra efectiv.
Mostra stratificata auditeaza FILTRUL, nu certifica setul.

NU SE STERGE NIMIC. Fiecare interogare primeste o dispozitie si un motiv, inclusiv cele excluse:
o intrebare medicala nu dispare, e RUTATA spre benchmarkul de safety; un follow-up contextual
ramane consemnat cu motivul excluderii. Un corpus din care ai sters ce nu-ti convenea nu mai poate
fi auditat de nimeni.

    python scripts/nx203_triage_queries.py --out tests/golden/qrels_manifest_v1.json
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

DEMO = "6098812a-50fc-44bd-a1ba-bc77e6399158"
MANIFEST_VERSION = 1
TARGET = 190

# --- criterii de triaj, SCRISE (nu implicite in cod) ------------------------------------------
# Fiecare tipar are un cod de motiv; codul ajunge in manifest, deci orice excludere e explicabila
# fara sa deschizi scriptul.
RULES: list[tuple[str, str, re.Pattern[str]]] = [
    (
        "pii_rejected",
        "contine identificator de persoana (email/telefon/card)",
        re.compile(r"[\w.+-]+@[\w-]+|\b0\d{9}\b|\b\d{13,19}\b"),
    ),
    (
        "safety_route",
        "cerere medicala / de siguranta — apartine benchmarkului de safety, nu celui de relevanta",
        re.compile(
            r"\b(insarcinat[aă]|sarcin[aă]|alaptez|alăptez|eczem[aă]|dermatit[aă]|psoriazis|"
            r"sanger|sânger|infectie|infecție|medicament|tratament medical|reteta|rețetă|"
            r"dermatolog|medic\b|alergie sever[aă])\b",
            re.I,
        ),
    ),
    (
        "prompt_injection",
        "incercare de manipulare a instructiunilor — apartine suitei red-team",
        re.compile(r"\b(ignora|ignoră)\s+(instructiunile|instrucțiunile|tot)", re.I),
    ),
    (
        "contextual",
        "depinde de un tur anterior (deictic, ordinal, confirmare, comparativ fara antecedent)",
        re.compile(
            r"^(da|nu|ok|si |și |apoi|atunci|mersi|multumesc|mulțumesc)\b"
            r"|\b(prima|primul|primele|a doua|al doilea|al treilea|astea|acestea|ele\b|asta|aia|"
            r"acela|acelasi|același)\b"
            r"|^(ai si|ai și|dar |mai )"
            r"|\bai (zis|spus|aratat|arătat)\b",
            re.I,
        ),
    ),
    (
        "non_retrieval",
        "alta intentie decat cautarea de produs (comanda, retur, handoff, cont)",
        re.compile(
            r"\b(comand|comenzi|retur|factur|awb|colet|livrare|curier|operator|consultant|agent|"
            r"garantie|garanție|rambur|plata|plată|cont\b|parola|reclamat|anulare|anulez|voucher|"
            r"cupon|politica|politică)\b",
            re.I,
        ),
    ),
    (
        "cart_action",
        "actiune de cos/checkout, nu interogare de cautare",
        re.compile(r"\b(adauga|adaugă|cos\b|coș\b|cumpar|cumpăr|link|checkout|finalizez)\b", re.I),
    ),
]

# Comparativ fara produs numit = referinta la ceva discutat anterior („mai ieftin" decat CE?).
_COMPARATIV = re.compile(r"\b(mai (ieftin|scump|bun|mare|mic)|cel mai (ieftin|scump|bun))\b", re.I)
_PRODUS = re.compile(
    r"\b(ser|serum|crema|cremă|sampon|șampon|masca|mască|fond|ruj|balsam|gel|lotiune|loțiune|"
    r"ulei|spray|pudra|pudră|rimel|mascara|tonic|exfoliant|protectie|protecție|deodorant|scrub|"
    r"primer|anticearcan|anticearcăn|fard|demachiant|apa micelara|apă micelară)\b",
    re.I,
)
# Prea scurt ca sa contina o intentie verificabila.
_MIN_LEN = 12


def triage(text: str) -> tuple[str, str]:
    """(dispozitie, motiv). `candidate` = propus pentru retrieval; restul, excluse CU motiv."""
    q = text.strip()
    if len(q) < _MIN_LEN:
        return "too_short", f"sub {_MIN_LEN} caractere — fara intentie verificabila"
    for disposition, reason, pattern in RULES:
        if pattern.search(q):
            return disposition, reason
    if _COMPARATIV.search(q) and not _PRODUS.search(q):
        return "contextual", "comparativ fara produs numit — se refera la un rezultat anterior"
    if not _PRODUS.search(q):
        # Nu e o respingere: e o marturisire ca filtrul nu se poate pronunta.
        return "needs_human", "nu numeste niciun tip de produs — eligibilitatea nu e clara mecanic"
    return "candidate", "de sine statatoare, numeste un tip de produs, fara semnale de excludere"


def _bucket(text: str) -> float:
    """[0,1) determinist din text — selectie reproductibila, fara random."""
    return int(hashlib.sha256(text.encode("utf-8")).hexdigest()[:8], 16) / 0xFFFFFFFF


async def _real_traffic() -> list[dict]:
    from src.db.connection import close_pool, tenant_conn  # noqa: PLC0415

    async with tenant_conn(DEMO) as conn:
        rows = await conn.fetch(
            "select lower(btrim(body)) as b, count(*) as n from messages "
            "where business_id=$1::uuid and direction='inbound' and body is not null "
            "and length(btrim(body)) between 6 and 200 group by 1 order by 1",
            DEMO,
        )
    await close_pool()
    return [{"text": r["b"], "source": "real_traffic", "seen": r["n"]} for r in rows]


def _qa_suite(path: pathlib.Path) -> list[dict]:
    import openpyxl  # noqa: PLC0415

    if not path.exists():
        print(f"  ! qa-suite lipseste ({path}) — se continua doar cu traficul real")
        return []
    wb = openpyxl.load_workbook(path, read_only=True)
    ws = wb["WB02"]
    it = ws.iter_rows(min_row=2, values_only=True)
    hdr = list(next(it))
    idx = {h: i for i, h in enumerate(hdr) if h}
    retrieval_cats = {
        "Product Search",
        "Product Recommendation",
        "Product Comparison",
        "Pricing",
        "Inventory",
    }
    out, seen = [], set()
    for r in it:
        if r[idx["Speaker"]] != "Customer" or r[idx["Category"]] not in retrieval_cats:
            continue
        if str(r[idx["Turn #"]]) != "1":  # doar primul tur e de sine statator prin constructie
            continue
        msg = r[idx["Message"]]
        if not isinstance(msg, str) or msg.strip() in seen:
            continue
        seen.add(msg.strip())
        out.append({"text": msg.strip(), "source": "qa_suite", "qa_category": r[idx["Category"]]})
    wb.close()
    return out


def select_stratified(candidates: list[dict], target: int) -> list[dict]:
    """Selectie STRATIFICATA pe sursa, determinista. Traficul real are prioritate: e singurul care
    poate purta provenienta `real_sanitized`, ceruta per categorie de poarta."""
    by_source: dict[str, list[dict]] = {}
    for c in candidates:
        by_source.setdefault(c["source"], []).append(c)
    for items in by_source.values():
        items.sort(key=lambda c: (_bucket(c["text"]), c["text"]))

    picked: list[dict] = []
    real = by_source.get("real_traffic", [])
    picked.extend(real[:target])  # traficul real, tot ce incape
    rest = target - len(picked)
    if rest > 0:
        picked.extend(by_source.get("qa_suite", [])[:rest])
    return picked


async def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        if sys.platform == "win32" and hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description="Triaj de eligibilitate NX-203")
    ap.add_argument(
        "--out", type=pathlib.Path, default=ROOT / "tests/golden/qrels_manifest_v1.json"
    )
    ap.add_argument(
        "--qa-suite",
        type=pathlib.Path,
        default=pathlib.Path("D:/Work/Sales Ass/qa-suite/02_End_To_End_Conversations.xlsx"),
    )
    ap.add_argument("--target", type=int, default=TARGET)
    args = ap.parse_args()

    rows = await _real_traffic()
    rows += _qa_suite(args.qa_suite)
    print(f"intrari brute: {len(rows)}")

    for row in rows:
        row["disposition"], row["reason"] = triage(row["text"])

    counts: dict[str, int] = {}
    for row in rows:
        counts[row["disposition"]] = counts.get(row["disposition"], 0) + 1
    print("\ndispozitii (NIMIC nu se sterge):")
    for k in sorted(counts, key=lambda x: -counts[x]):
        print(f"  {counts[k]:4d}  {k}")

    candidates = [r for r in rows if r["disposition"] == "candidate"]
    selected = select_stratified(candidates, args.target)
    sel_texts = {c["text"] for c in selected}
    for row in rows:
        row["selected_for_retrieval"] = row["text"] in sel_texts
        # Eligibilitatea NU e stabilita de script: ramane de confirmat, per intrare selectata.
        row["eligibility_confirmed"] = False

    print(f"\ncandidati: {len(candidates)}  ->  selectati: {len(selected)} (tinta {args.target})")
    if len(selected) < args.target:
        print(f"  ! LIPSA {args.target - len(selected)} — nu se completeaza tacut cu excluse")

    args.out.write_text(
        json.dumps(
            {
                "manifest_version": MANIFEST_VERSION,
                "business_id": DEMO,
                "target": args.target,
                "_method": (
                    "triaj mecanic -> dispozitie + motiv. NU e verdict de eligibilitate: "
                    "`eligibility_confirmed` se pune de om, per intrare selectata."
                ),
                "counts": counts,
                "entries": rows,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"\nmanifest: {args.out}")
    return 0


raise SystemExit(asyncio.run(main()))
