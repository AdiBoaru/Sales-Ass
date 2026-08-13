"""NX-235 — manual drive: o conversație de 12 tururi prin reducerul și resolverul REALE.

De ce un harness și nu doar teste: testele verifică reguli izolate, dar întrebarea „memoria asta
se comportă rezonabil într-o conversație?" se pune pe o secvență citită de un om. Scriptul rulează
scenariul cerut de card — buget + nevoie explicită, recomandare, „al doilea", navigare pe alt PDP,
corectarea bugetului, revocarea nevoii, schimbare de subiect, siguranță explicită, referință
ambiguă — și tipărește după fiecare tur DOAR proiecția SAFE a stării.

Zero OpenAI, zero DB, zero rețea: reducerul și resolverul sunt pure, deci conversația e o listă de
propuneri. Rulare:

    python scripts/state_v2_drive.py            # narativ, cu explicații
    python scripts/state_v2_drive.py --json     # o linie JSON per tur (diff-abil în PR)
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Consola Windows pornește pe cp1252: diacriticele și săgețile din raport ar arunca
# `UnicodeEncodeError` exact în scriptul care trebuie citit de un om.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from src.agent.reference_resolver import (  # noqa: E402
    ActionAnchor,
    PageAnchor,
    ReferenceRequest,
    resolve_reference,
)
from src.conversation.needs import NeedVocabulary  # noqa: E402
from src.conversation.state_reducer import (  # noqa: E402
    ReducerPolicy,
    StateUpdateProposal,
    reduce_all,
)
from src.conversation.state_v2 import (  # noqa: E402
    ConversationStateV2,
    serialize,
    size_bucket,
)

VOCAB = NeedVocabulary.from_pack(None)
POLICY = ReducerPolicy(vocabulary=VOCAB, sensitive_consent=True)


@dataclass(frozen=True)
class Ref:
    product_id: str
    name: str


@dataclass
class Turn:
    """Un tur: ce a scris clientul, ce PROPUN stagiile și (opțional) ce referință se rezolvă."""

    utterance: str
    proposals: list[StateUpdateProposal] = field(default_factory=list)
    refs: tuple[Ref, ...] = ()
    page: PageAnchor | None = None
    anchor: ActionAnchor | None = None
    note: str = ""


LIST_A = (Ref("p1", "Ser cu vitamina C"), Ref("p2", "Cremă hidratantă Petală"))
LIST_B = (Ref("p7", "Laptop Aur 14"), Ref("p8", "Laptop Aur 16"))
PDP = PageAnchor("p3", "Ser cu retinol 0.3%")


def user(op: str, **kw) -> StateUpdateProposal:
    return StateUpdateProposal(op, source="user_explicit", **kw)  # type: ignore[arg-type]


SCENARIO: list[Turn] = [
    Turn(
        "caut un ser pentru acnee, sub 150 lei",
        [
            StateUpdateProposal("set_topic", category_key="seruri", source="catalog"),
            user("set_need", key="budget_max", value=150),
            user("set_need", key="concerns", value="acnee"),
        ],
        note="buget + nevoie explicită → hard + soft, legate de subiectul curent",
    ),
    Turn(
        "(asistentul recomandă două seruri)",
        [
            StateUpdateProposal(
                "set_references",
                source="catalog",
                payload={
                    "displayed_products": [
                        {"product_id": r.product_id, "name": r.name} for r in LIST_A
                    ]
                },
            )
        ],
        refs=LIST_A,
        note="setul afișat primește o REVIZIE — ancora oricărui ordinal de acum înainte",
    ),
    Turn("spune-mi de a doua", [], refs=LIST_A, note="ordinal peste lista curentă"),
    Turn(
        "ce părere ai despre acesta?",
        [],
        refs=LIST_A,
        page=PDP,
        note="deictic pe alt PDP → ancora paginii bate setul afișat",
    ),
    Turn(
        "care are rating mai bun?",
        [],
        refs=LIST_A,
        page=PDP,
        note="fără deixis → pagina NU confiscă întrebarea; ambiguu, nu o ghicire",
    ),
    Turn(
        "de fapt pot merge până la 300",
        [user("set_need", key="budget_max", value=300)],
        note="CORECȚIE: valoarea veche devine superseded + tombstone",
    ),
    Turn(
        "(rezumatul reafirmă bugetul vechi de 150)",
        [StateUpdateProposal("set_need", key="budget_max", value=150, source="model_inferred")],
        note="modelul nu poate rescrie un hard → respins",
    ),
    Turn(
        "acneea nu mai e o problemă",
        [user("revoke", key="concerns")],
        note="REVOCARE explicită → tombstone",
    ),
    Turn(
        "(istoricul reintroduce acneea)",
        [StateUpdateProposal("set_need", key="concerns", value="acnee", source="model_inferred")],
        note="cheie revocată + sursă non-client → respins; aici se închide bucla",
    ),
    Turn(
        "sunt însărcinată",
        [
            StateUpdateProposal(
                "set_need",
                key="restriction",
                value="retinol",
                source="policy",
                sensitive_class="health",
            )
        ],
        note="context de siguranță (NX-173) → hard, sensibil, ne-relaxabil",
    ),
    Turn(
        "de fapt caut un laptop",
        [
            StateUpdateProposal("set_topic", category_key="laptopuri", source="catalog"),
            StateUpdateProposal(
                "set_references",
                source="catalog",
                payload={
                    "displayed_products": [
                        {"product_id": r.product_id, "name": r.name} for r in LIST_B
                    ]
                },
            ),
        ],
        refs=LIST_B,
        note="topic switch: cad nevoile scope-uite, siguranța RĂMÂNE; lista afișată se schimbă",
    ),
    Turn(
        "(click pe un card dintr-o listă veche)",
        [],
        refs=LIST_B,
        page=PDP,
        anchor=ActionAnchor("p1", revision=2),
        note="ancoră emisă peste altă revizie → STALE, nu fallback tăcut",
    ),
]


def safe_projection(state: ConversationStateV2) -> dict:
    """Ce are voie să vadă un om care depanează: chei canonice, tării, statusuri. Fără text brut,
    fără PII, fără valori sensibile — exact ce poate ajunge și într-un log."""
    doc, size, degraded = serialize(state)
    return {
        "revision": state.revision,
        "topic": state.topic.category_key,
        "needs": [
            {
                "key": n.key,
                "op": n.operator,
                "value": ("<sensibil>" if n.sensitive_class else n.normalized_value),
                "strength": n.strength,
                "source": n.source,
            }
            for n in state.active_needs()
        ],
        "revoked": sorted(state.revoked_keys()),
        "pending": state.pending_clarification.target_key if state.pending_clarification else None,
        "displayed": [d.product_id for d in state.references.displayed_products],
        "displayed_revision": state.references.displayed_revision,
        "size_bucket": size_bucket(size),
        "degraded": degraded,
    }


def drive(as_json: bool) -> int:
    state = ConversationStateV2()
    failures: list[str] = []

    resolutions: list[tuple[int, str, str]] = []
    for index, turn in enumerate(SCENARIO, start=1):
        reduced = reduce_all(state, turn.proposals, POLICY, revision=state.revision + 1)
        state = reduced.state
        resolution = None
        if turn.refs or turn.page or turn.anchor:
            resolution = resolve_reference(
                ReferenceRequest(
                    query=turn.utterance,
                    refs=turn.refs,
                    page=turn.page,
                    anchor=turn.anchor,
                    selected_product=state.references.selected_product,
                    displayed_revision=state.references.displayed_revision,
                )
            )
        if resolution is not None:
            resolutions.append((index, resolution.outcome, resolution.reason))
        projection = safe_projection(state)
        record = {
            "turn": index,
            "utterance": turn.utterance,
            "state": projection,
            "rejected": [{"op": r.op, "reason": r.reason} for r in reduced.rejected],
            "reference": (
                {
                    "source": resolution.source,
                    "outcome": resolution.outcome,
                    "reason": resolution.reason,
                }
                if resolution
                else None
            ),
        }
        if as_json:
            print(json.dumps(record, ensure_ascii=False))
        else:
            print(f"\n── turul {index}: {turn.utterance}")
            if turn.note:
                print(f"   ({turn.note})")
            print(f"   revizie {projection['revision']} · subiect {projection['topic']}")
            for need in projection["needs"]:
                mark = f"[{need['strength']}/{need['source']}]"
                print(f"   • {need['key']} {need['op']} {need['value']}  {mark}")
            if projection["revoked"]:
                print(f"   retrase: {', '.join(projection['revoked'])}")
            for rejected in record["rejected"]:
                print(f"   RESPINS {rejected['op']}: {rejected['reason']}")
            if resolution:
                print(
                    f"   referință → {resolution.source}/{resolution.outcome} "
                    f"({resolution.reason}) = {resolution.product_id}"
                )
            print(f"   stare: {projection['size_bucket']}, degradată={projection['degraded']}")

    # Invariantele pe care le verificăm MECANIC la final — un drive care doar tipărește frumos
    # nu dovedește nimic. Fiecare linie corespunde unui rând din failure matrix.
    if state.need_for("concerns") is not None:
        failures.append("nevoia revocată a reapărut")
    if state.need_for("restriction") is None:
        failures.append("siguranța a dispărut la topic switch")
    if state.need_for("budget_max") is not None:
        failures.append("bugetul scope-uit n-a fost retras la schimbarea de subiect")
    if state.pending_clarification is not None:
        failures.append("a rămas o întrebare zombi în așteptare")
    if safe_projection(state)["degraded"]:
        failures.append("starea a depășit bugetul și a degradat")
    outcomes = {index: (outcome, reason) for index, outcome, reason in resolutions}
    if outcomes.get(len(SCENARIO), ("", ""))[0] != "stale":
        failures.append("ancora emisă peste o listă veche n-a fost respinsă ca stale")
    if outcomes.get(5, ("", ""))[0] != "ambiguous":
        failures.append("o întrebare fără deixis a fost ancorată tăcut pe pagina curentă")

    if as_json:
        # Verdictul e tot o linie JSON: `--json` trebuie să rămână parsabil integral, altfel
        # snapshotul din PR nu se poate diffa fără să tai coada manual.
        print(json.dumps({"verdict": "fail" if failures else "ok", "failures": failures}))
        return 1 if failures else 0

    print()
    if failures:
        for failure in failures:
            print(f"FAIL: {failure}")
        return 1
    print(
        "OK — revocările nu revin, siguranța rămâne, nevoile scope-uite s-au retras, stare sub cap."
    )
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="o linie JSON per tur")
    raise SystemExit(drive(parser.parse_args().json))
