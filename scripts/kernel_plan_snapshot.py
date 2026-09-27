"""NX-333 — snapshot-urile golden ale plannerului kernelului (`kernel.v1.1`, pasul 4b).

Pentru fiecare pachet (patru de fixture + SOLE), fiecare journey din `tests/golden/kernel_journeys/`
rulează ÎN LANȚ prin kernelul real (validator → resolver → delta → reducer → poartă → planner, zero
model, zero DB, `tests/kernel/fixture_catalog.py`), iar planul COMPLET al fiecărui tur (toate
planurile, cu `SearchArgs`, golurile, dezvăluirile, actele nefăcute) se scrie în
`tests/kernel/plans/<pachet>.json`. Orice diferență pică verificarea, până la o regenerare
conștientă: o schimbare a unui strat de dinainte (poarta, reducerul, proveniența) care mută un
`SearchArgs` se vede aici, nu în producție.

    python scripts/kernel_plan_snapshot.py            # verifică (exit 1 la diferență)
    python scripts/kernel_plan_snapshot.py --write    # rescrie snapshot-urile

`limit` lipsește din snapshot: plannerul nu-l scrie (e implicitul uneltei, `card_slots`), iar o
configurație locală diferită n-are voie să schimbe verdictul. Rulează cu setările suitei (fără
`.env`-ul dezvoltatorului), ca `tests/conftest.py`."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Aceleași valori ca `tests/conftest.py`: snapshot-ul măsoară plannerul, nu configurația locală.
if not os.getenv("NX_TESTS_READ_ENV_FILE"):
    os.environ["NX_CONFIG_ENV_FILE"] = ""
    for _key, _value in {
        "OPENAI_API_KEY": "test-key",
        "SUPABASE_DB_URL": "postgresql://test:test@localhost/test",
        "REDIS_URL": "redis://localhost:6379/0",
        "ENV": "test",
        "LOG_LEVEL": "WARNING",
        "DAILY_COST_CAP_USD": "5",
    }.items():
        os.environ.setdefault(_key, _value)

PLANS_DIR = ROOT / "tests" / "kernel" / "plans"


def _plan_doc(plan: Any) -> dict[str, Any]:
    doc: dict[str, Any] = {"executor": plan.executor}
    if plan.product_ids:
        doc["product_ids"] = list(plan.product_ids)
    if plan.search_args is not None:
        args = plan.search_args.model_dump(mode="json", exclude_defaults=True)
        args.pop("limit", None)
        doc["search_args"] = args
    if plan.depends_on is not None:
        doc["depends_on"] = plan.depends_on
    return doc


def snapshots() -> dict[str, dict[str, Any]]:
    """`pachet → {journey_id: [tur...]}`, fiecare tur cu planul lui complet. PUR peste fixture."""
    from tests.kernel import fixture_catalog, replay  # noqa: PLC0415 — după setările suitei

    out: dict[str, dict[str, Any]] = {name: {} for name in (*replay.FIXTURE_PACKS, "sole-ro")}
    for journey in replay.load_journeys():
        turns = []
        for index, turn in enumerate(journey.turns):
            planned = fixture_catalog.planned_turn(journey, index)
            turns.append(
                {
                    "user_input": turn.user_input,
                    "plans": [_plan_doc(p) for p in planned.plans],
                    "primary": planned.primary,
                    "dropped_acts": planned.dropped_acts,
                    "gaps": list(planned.gaps),
                    "disclosures": [list(d) for d in planned.disclosures],
                }
            )
        out.setdefault(journey.pack, {})[journey.journey_id] = turns
    return out


def dump(doc: dict[str, Any]) -> str:
    return json.dumps(doc, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def differences() -> list[str]:
    """Pachetele al căror snapshot diferă de planurile de acum (gol = conform)."""
    problems = []
    current = snapshots()
    for name, doc in current.items():
        path = PLANS_DIR / f"{name}.json"
        stored = path.read_text(encoding="utf-8").replace("\r\n", "\n") if path.exists() else ""
        if stored != dump(doc):
            problems.append(name)
    extra = {p.stem for p in PLANS_DIR.glob("*.json")} - set(current)
    problems += sorted(f"{name} (fără journey)" for name in extra)
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--write", action="store_true", help="rescrie snapshot-urile")
    args = parser.parse_args(argv)
    if args.write:
        PLANS_DIR.mkdir(parents=True, exist_ok=True)
        for name, doc in snapshots().items():
            (PLANS_DIR / f"{name}.json").write_text(dump(doc), encoding="utf-8", newline="\n")
            print(f"scris: {(PLANS_DIR / f'{name}.json').relative_to(ROOT).as_posix()}")
        return 0
    problems = differences()
    if problems:
        print(
            "planurile au divergat de snapshot: "
            + ", ".join(problems)
            + ". Dacă schimbarea e voită, rulează `python scripts/kernel_plan_snapshot.py --write`"
            " și revizuiește diff-ul.",
            file=sys.stderr,
        )
        return 1
    print("OK: planurile = snapshot-urile golden")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
