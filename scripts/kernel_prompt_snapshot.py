"""NX-335 — snapshot-urile promptului de interpretare (`kernel.v1.1`, pasul 5), pe fixture.

Riscul din designul kernelului (§I): modelul descrie schimbări față de o stare RANDATĂ, deci un bug
de randare devine un bug de interpretare. Pentru fiecare pachet de fixture, o stare reală a
kernelului (construită prin lanțul real din `tests/kernel/fixture_catalog.py`, pe journey-urile
etichetate, zero model, zero DB) se randează prin adaptor, iar promptul COMPLET (system + user) se
scrie în `tests/kernel/prompts/<pachet>.txt`. Orice diferență pică verificarea, până la o regenerare
conștientă.

    python scripts/kernel_prompt_snapshot.py            # verifică (exit 1 la diferență)
    python scripts/kernel_prompt_snapshot.py --write    # rescrie snapshot-urile

SOLE nu intră: vocabularul și meniul lui vin din DB, iar pachetul de producție stă în
`businesses.settings`. Promptul SOLE real îl tipărește `scripts/nx335_interpret_smoke.py`.

Starea aleasă per pachet e DERIVATĂ, nu scrisă de mână: dintre prefixele journey-urilor pachetului,
cel care acoperă cele mai multe blocuri ale vederii (nevoi, ecran, seturi de mai devreme, parcat,
întrebare în așteptare); la egalitate, primul în ordinea corpusului. Mesajul curent e turul care
urmează prefixului, iar replicile botului sunt numele arătate în tur (date, nu text în cod)."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Aceleași valori ca `tests/conftest.py`: snapshot-ul măsoară adaptorul, nu configurația locală.
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

PROMPTS_DIR = ROOT / "tests" / "kernel" / "prompts"


def _coverage(state: Any) -> int:
    """Câte blocuri ale vederii are starea pline (nevoile contează până la două)."""
    refs = state.references
    return (
        min(len(state.active_needs()), 2)
        + int(bool(refs.displayed_products))
        + int(bool(refs.recent_sets))
        + int(state.parked is not None)
        + int(state.pending_clarification is not None)
    )


def snapshot_input(name: str) -> Any:
    """`InterpretInput`-ul de snapshot al pachetului `name`. PUR peste fixture."""
    from src.conversation.state_v2 import ConversationStateV2  # noqa: PLC0415
    from src.conversation.turn_interpreter import InterpretInput, display  # noqa: PLC0415
    from tests.kernel import fixture_catalog as fc  # noqa: PLC0415 — după setările suitei
    from tests.kernel import replay  # noqa: PLC0415

    best: tuple[int, Any] | None = None
    catalog = fc.products(name)
    for journey in replay.load_journeys():
        if journey.pack != name or len(journey.turns) < 2:
            continue
        state = ConversationStateV2()
        history: list[tuple[str, str]] = []
        for i, turn in enumerate(journey.turns[:-1]):
            step = fc.kernel_step(
                name,
                state,
                turn.expect["interpretation"],
                turn.user_input,
                earlier=tuple(t.user_input for t in journey.turns[:i])[::-1],
                shown_ids=turn.shown,
                turn_id=f"t{i}",
                locale=journey.locale,
            )
            state = step.state_after
            history.append(("user", turn.user_input))
            if turn.shown:
                history.append(("bot", ". ".join(display(catalog[p]["name"]) for p in turn.shown)))
            score = _coverage(state)
            if best is None or score > best[0]:
                best = (
                    score,
                    InterpretInput(
                        locale=journey.locale,
                        pack=fc.pack(name),
                        vocab=fc.vocabulary(name),
                        category_menu=fc.category_menu(name),
                        state=state,
                        history=tuple(history),
                        message=journey.turns[i + 1].user_input,
                    ),
                )
    if best is None:
        raise SystemExit(f"{name}: niciun journey de cel puțin două ture")
    return best[1]


def render(name: str) -> str:
    from src.conversation import turn_interpreter as ti  # noqa: PLC0415

    inp = snapshot_input(name)
    return f"=== SYSTEM ===\n{ti.system_prompt(inp)}\n\n=== USER ===\n{ti.user_prompt(inp)}\n"


def snapshots() -> dict[str, str]:
    from tests.kernel import replay  # noqa: PLC0415

    return {name: render(name) for name in replay.FIXTURE_PACKS}


def differences() -> list[str]:
    """Pachetele al căror snapshot diferă de promptul de acum (gol = conform)."""
    problems = []
    current = snapshots()
    for name, text in current.items():
        path = PROMPTS_DIR / f"{name}.txt"
        stored = path.read_text(encoding="utf-8").replace("\r\n", "\n") if path.exists() else ""
        if stored != text:
            problems.append(name)
    extra = {p.stem for p in PROMPTS_DIR.glob("*.txt")} - set(current)
    problems += sorted(f"{name} (fără pachet)" for name in extra)
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--write", action="store_true", help="rescrie snapshot-urile")
    args = parser.parse_args(argv)
    if args.write:
        PROMPTS_DIR.mkdir(parents=True, exist_ok=True)
        for name, text in snapshots().items():
            (PROMPTS_DIR / f"{name}.txt").write_text(text, encoding="utf-8", newline="\n")
            print(f"scris: {(PROMPTS_DIR / f'{name}.txt').relative_to(ROOT).as_posix()}")
        return 0
    problems = differences()
    if problems:
        print(
            "promptul de interpretare a divergat de snapshot: "
            + ", ".join(problems)
            + ". Dacă schimbarea e voită, rulează `python scripts/kernel_prompt_snapshot.py"
            " --write` și revizuiește diff-ul.",
            file=sys.stderr,
        )
        return 1
    print("OK: promptul de interpretare = snapshot-urile")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
