"""NX-335 felia 5c — raportul de acord al adaptorului de interpretare pe turele reale.

Întrebarea: citește modelul turele reale suficient de bine încât cablarea de la pasul 6 să merite?
Pași, fiecare cu modul lui:

1. `--snapshot` (ZERO apeluri de model, DB read-only): instantaneul corpusului, ÎNAINTE ca retenția
   `conversation_traces` (30 de zile) să-l șteargă. Pentru fiecare tur: textul clientului și al
   botului, ce a arătat v1 (`recommended`) și argumentele căutării v1 (`analytics_events`
   `tool_call` + `product_search`, pe `turn_id`); lângă el pachetul, meniul de rafturi și
   vocabularul din ziua instantaneului (cu `vocabulary_snapshot`), ca rularea să rezolve pe același
   vocabular oricând ar porni. Totul local, în `reports/nx335/`, NU în repo (texte de client).
2. implicit (`--dry-run`, ZERO apeluri): corpusul din instantaneu, tokenii estimați și costul.
3. `--yes`: rularea reală, un apel pe tur pe braț de efort (`--efforts none` implicit;
   `none,low` rulează ambele, cu ordinea amestecată per tur, tiparul NX-312 felia 5). **Consumă
   credite OpenAI; o pornește Adi.**

Reconstrucția turului trece prin funcțiile PRODUCȚIEI, în lanț pe conversație: starea kernelului
pornește goală, iar fiecare tur trece prin `validate` → `resolve_references` (faptele recitite din
catalog, `reference_facts`, read-only) → `to_delta` → `reduce_turn`; sursele resolverului vin din
`references.sources_from_state`. Ecranul e al lui v1: kernelul nu execută aici, deci scriptul
sintetizează din `recommended` EXACT propunerea pe care o emite producția (`set_references`,
`source="catalog"`, deci `recent_sets` se rotește ca acolo). Poarta și plannerul rulează doar
pentru comparația cu ce a căutat v1. Fiecare braț își ține propriul lanț de stare.

Limita, declarată: ecranul e al lui v1, nu al planului kernelului, iar o greșeală la turul 2 se
propagă în starea turului 3; de aceea raportul dă și primul tur divergent per conversație.

NX-339 (măsurătoarea pentru promptul `interpret.v2`):

4. `--journeys`: setul NEVĂZUT. Journey-urile kernelului (`tests/golden/kernel_journeys`) pe
   catalogul lor de fixture, fără DB; starea fiecărui tur vine din `stage_harness.chain` (din
   interpretările AȘTEPTATE, deci o greșeală nu se propagă), iar eticheta e interpretarea așteptată
   trecută prin ACELAȘI pas pur ca a modelului. Un câmp absent din journey nu se evaluează. Seturi:
   `B` (pachetele pe care promptul nu se reglează) și `A-bis` (`sole-ro`). Tipărește doar agregate.
   `--journeys-dir DIR [--set-name C]`: alt set nevăzut (setul C, scris separat pentru verdictul
   promptului v3), cu numele setului pe toate turele; raportul în `journeys-c/`.
5. `--regressions ÎNAINTE DUPĂ`: turele care trec din corect în greșit între două `results.json`.

Rândurile păstrează interpretarea brută și verdictul validatorului pe fiecare schimbare (local, sub
`reports/nx335/`, ignorat de git).

    PYTHONPATH=. python scripts/nx335_interpret_replay.py --business sole-ro --snapshot
    PYTHONPATH=. python scripts/nx335_interpret_replay.py --business sole-ro            # dry-run
    PYTHONPATH=. python scripts/nx335_interpret_replay.py --business sole-ro --yes
    PYTHONPATH=. python scripts/nx335_interpret_replay.py --efforts none,low --yes
    PYTHONPATH=. python scripts/nx335_interpret_replay.py --journeys [--yes]
    PYTHONPATH=. python scripts/nx335_interpret_replay.py --regressions a.json b.json
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import math
import random
import sys
from collections import Counter
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from src.agent import usage  # noqa: E402
from src.agent.turn_planner import PlannedTurn, plan_turn  # noqa: E402
from src.catalog.subject_pairs import mark_pairs, subject_type_pairs  # noqa: E402
from src.catalog.vocabulary import CatalogVocabulary, VocabEntry  # noqa: E402
from src.config import INTERPRET_EFFORTS  # noqa: E402
from src.conversation.ambiguity_gate import (  # noqa: E402
    GateOutcome,
    decide_ambiguity,
    lookup_attributes,
)
from src.conversation.clarification_policy import ClarificationPolicy  # noqa: E402
from src.conversation.delta import TurnDelta, to_delta  # noqa: E402
from src.conversation.interpretation import (  # noqa: E402
    ResolvedRef,
    TurnInterpretation,
)
from src.conversation.interpretation_check import (  # noqa: E402
    Validated,
    format_value,
    snapshot_id,
    validate,
)
from src.conversation.needs import NeedVocabulary  # noqa: E402
from src.conversation.provenance import UserWords, need_handles  # noqa: E402
from src.conversation.references import (  # noqa: E402
    CatalogLookup,
    ReferenceFacts,
    plan_lookup,
    resolve_references,
    sources_from_state,
)
from src.conversation.state_reducer import (  # noqa: E402
    ReducerPolicy,
    StateUpdateProposal,
    reduce_turn,
)
from src.conversation.state_v2 import ConversationStateV2  # noqa: E402
from src.conversation.turn_interpreter import (  # noqa: E402
    InterpretedTurn,
    InterpretInput,
    interpret_turn,
    system_prompt,
    user_prompt,
    user_words,
)
from src.models import BusinessConfig  # noqa: E402

OUT_DIR = ROOT / "reports" / "nx335"
LABELS_DIR = ROOT / "tests" / "golden" / "kernel_real"
CORPUS_FILE = "corpus.jsonl"
VOCAB_FILE = "vocabulary.json"
#: Estimare grosieră de tokeni (≈ 4 caractere pe token), declarată: tokenizerul real e al
#: furnizorului, iar raportul real citește tokenii din `per_call`.
CHARS_PER_TOKEN = 4
#: Ieșirea estimată pe tur (cardul, „Cost"): o interpretare are ~400 de tokeni.
EST_OUTPUT_TOKENS = 400
#: Pragurile PREÎNREGISTRATE din card (propunere; le ratifică Adi înainte de rulare).
THRESHOLDS = {
    "primary_act": 0.90,
    "thread": 0.95,
    "changes_f1": 0.85,
    "unknown_reference_max": 0.02,
    # NX-345 D1 (Adi, 2026-09-29): ipotezele (`implicit`/`inferred`) sunt neutre în F1, dar au
    # metrica lor: cât din ele numesc ALTĂ valoare pe o dimensiune etichetată.
    "hypotheses_contradicted_max": 0.15,
    # NX-345a: ipotezele NEETICHETATE pe tur cu schimbări evaluate (linia de bază pe A: 0,133-0,144;
    # marjă 25%, fixată înaintea rulării v4).
    "unlabelled_hypotheses_per_turn_max": 0.18,
}
#: NX-345 D1: proveniențele pe care kernelul NU le face fapt. `inferred` e doar semnal de ordonare,
#: nepersistat (I23), deci nici nu se potrivește cu eticheta: starea nu-l poartă. `implicit` se
#: persistă ca nevoie `soft` (nu filtrează), deci se potrivește, dar neetichetat e neutru.
HYPOTHESIS_PROVENANCE = frozenset({"implicit", "inferred"})
#: Recenzia NX-345a: pe dimensiunile care SCHIMBĂ SUBIECTUL, `implicit` e fapt: devine `set_topic`,
#: parchează subiectul, iar plannerul caută pe raftul nou. Acolo nu e ipoteză, ci pozitiv fals.
#: NX-348 a intrat în `main` ÎNAINTEA rulării v4, deci se aplică regula pre-înregistrată în
#: NX-339: tipul de produs mută și el subiectul (NX-331), deci e dimensiune de subiect.
SUBJECT_DIMENSIONS = frozenset({"category", "product_type"})
#: NX-350 (kernel.v4.0): pe ce dimensiune de subiect o schimbare `implicit` e totuși FAPT. Tipul
#: `implicit` devine doar umbrela subiectului (ordonează pe toate codurile cuvântului, nu pe
#: valoarea modelului), deci doar raftul. Umbrela nu se compară cu eticheta (rapoartele n-o poartă).
IMPLICIT_FACT_DIMENSIONS = frozenset({"category"})
GATE_POLICY = ClarificationPolicy()
FactsFn = Callable[[CatalogLookup], Awaitable[ReferenceFacts]]


# --- instantaneul (fișiere locale) ---------------------------------------------------------------


@dataclass(frozen=True)
class CorpusTurn:
    """Un tur real, din instantaneu. Textele clientului/botului rămân LOCALE (nu intră în repo)."""

    turn_id: str
    conversation_id: str
    seq: int
    created_at: str
    language: str
    client_text: str
    bot_text: str
    recommended: tuple[dict[str, Any], ...]
    tool_calls: tuple[dict[str, Any], ...] = ()
    product_search: tuple[dict[str, Any], ...] = ()


@dataclass(frozen=True)
class Snapshot:
    business: BusinessConfig
    pack: Any
    vocab: CatalogVocabulary
    category_menu: tuple[tuple[str, int], ...]
    vocabulary_snapshot: str
    turns: tuple[CorpusTurn, ...]

    def conversations(self) -> list[list[CorpusTurn]]:
        by: dict[str, list[CorpusTurn]] = {}
        for t in self.turns:
            by.setdefault(t.conversation_id, []).append(t)
        return [sorted(v, key=lambda t: (t.seq, t.created_at)) for _, v in sorted(by.items())]


def vocab_to_doc(vocab: CatalogVocabulary) -> dict[str, Any]:
    return {
        "business_id": vocab.business_id,
        "dimensions": {
            name: [[e.key, e.label, e.count, e.path] for e in entries]
            for name, entries in sorted(vocab.dimensions.items())
        },
        "noise_badges": sorted(vocab.noise_badges),
    }


def vocab_from_doc(doc: Mapping[str, Any]) -> CatalogVocabulary:
    return CatalogVocabulary(
        business_id=str(doc["business_id"]),
        dimensions={
            name: tuple(VocabEntry(str(k), str(lb), int(n), str(p or "")) for k, lb, n, p in rows)
            for name, rows in doc.get("dimensions", {}).items()
        },
        noise_badges=frozenset(doc.get("noise_badges", ())),
    )


def load_snapshot(directory: Path) -> Snapshot:
    """Instantaneul din `directory`, fără DB: pachetul se reconstruiește prin `load_domain_pack`
    din setările salvate, vocabularul din `vocabulary.json`."""
    from src.domain.loader import load_domain_pack  # noqa: PLC0415

    meta = json.loads((directory / VOCAB_FILE).read_text(encoding="utf-8"))
    business = BusinessConfig(**meta["business"])
    pack = load_domain_pack(business)
    turns = tuple(
        CorpusTurn(
            **{
                **row,
                "recommended": tuple(row.get("recommended") or ()),
                "tool_calls": tuple(row.get("tool_calls") or ()),
                "product_search": tuple(row.get("product_search") or ()),
            }
        )
        for row in (
            json.loads(line)
            for line in (directory / CORPUS_FILE).read_text(encoding="utf-8").splitlines()
            if line.strip()
        )
    )
    vocab = vocab_from_doc(meta["vocabulary"])
    # Recenzia NX-335: amprenta se RECALCULEAZĂ din pachetul și vocabularul salvate (funcția ei s-a
    # schimbat după instantaneu), iar valoarea nouă se scrie în metadate, lângă cea din ziua
    # capturii. Fără DB: vocabularul e cel salvat, nu cel de azi.
    current = snapshot_id(pack, vocab)
    if meta.get("vocabulary_snapshot") != current:
        meta.setdefault("vocabulary_snapshot_at_capture", meta.get("vocabulary_snapshot"))
        meta["vocabulary_snapshot"] = current
        (directory / VOCAB_FILE).write_text(
            json.dumps(meta, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
        )
    return Snapshot(
        business=business,
        pack=pack,
        vocab=vocab,
        category_menu=tuple((str(k), int(n)) for k, n in meta["category_menu"]),
        vocabulary_snapshot=current,
        turns=turns,
    )


async def take_snapshot(business_ref: str, out_dir: Path) -> dict[str, int]:
    """Instantaneul corpusului, pe DB, READ-ONLY. Zero apeluri de model.

    `conversation_traces` și `messages` pe `tenant_conn` (`business_id = $1`); `analytics_events`
    pe `admin_conn` cu `business_id` explicit (`bot_runtime` are doar INSERT pe tabel, prin
    design). Nicio scriere, în niciun tabel."""
    from src.catalog.vocabulary import load_vocabulary  # noqa: PLC0415
    from src.db.connection import admin_conn, close_pool, get_pool, tenant_conn  # noqa: PLC0415
    from src.db.queries.businesses import load_business  # noqa: PLC0415
    from src.db.queries.catalog import list_category_menu  # noqa: PLC0415

    try:
        pool = await get_pool()
        async with admin_conn(pool) as admin:
            bid = await admin.fetchval(
                "select id::text from businesses where slug = $1 or id::text = $1", business_ref
            )
            if bid is None:
                raise SystemExit(f"tenant necunoscut: {business_ref!r}")
            async with tenant_conn(bid) as conn:
                business = await load_business(conn, bid)
                vocab = await load_vocabulary(conn, bid)
                menu = await list_category_menu(conn, bid)
                traces = await conn.fetch(
                    """
                    select turn_id::text as turn_id, conversation_id::text as conversation_id,
                           created_at, coalesce(language, '') as language,
                           coalesce(client_text, '') as client_text,
                           coalesce(bot_text, '') as bot_text, recommended
                    from conversation_traces
                    where business_id = $1::uuid
                    order by conversation_id, created_at
                    """,
                    bid,
                )
                conv_ids = sorted({t["conversation_id"] for t in traces})
                messages = await conn.fetch(
                    """
                    select conversation_id::text as conversation_id, direction, author,
                           created_at, coalesce(body, '') as body
                    from messages
                    where business_id = $1::uuid and conversation_id::text = any($2::text[])
                    order by conversation_id, created_at
                    """,
                    bid,
                    conv_ids,
                )
            events = await admin.fetch(
                """
                select turn_id::text as turn_id, event_type, properties, created_at
                from analytics_events
                where business_id = $1::uuid and event_type in ('tool_call', 'product_search')
                  and turn_id::text = any($2::text[])
                order by created_at
                """,
                bid,
                [t["turn_id"] for t in traces],
            )
    finally:
        await close_pool()
    if business is None:
        raise SystemExit("businessul nu s-a putut încărca")

    by_turn: dict[str, dict[str, list[dict[str, Any]]]] = {}
    for e in events:
        props = e["properties"]
        props = json.loads(props) if isinstance(props, str) else dict(props or {})
        by_turn.setdefault(e["turn_id"], {}).setdefault(e["event_type"], []).append(props)

    out_dir.mkdir(parents=True, exist_ok=True)
    seq: Counter[str] = Counter()
    rows = []
    for t in traces:
        rec = t["recommended"]
        rec = json.loads(rec) if isinstance(rec, str) else list(rec or [])
        seq[t["conversation_id"]] += 1
        rows.append(
            {
                "turn_id": t["turn_id"],
                "conversation_id": t["conversation_id"],
                "seq": seq[t["conversation_id"]],
                "created_at": t["created_at"].isoformat(),
                "language": t["language"] or business.default_locale,
                "client_text": t["client_text"],
                "bot_text": t["bot_text"],
                "recommended": rec,
                "tool_calls": by_turn.get(t["turn_id"], {}).get("tool_call", []),
                "product_search": by_turn.get(t["turn_id"], {}).get("product_search", []),
            }
        )
    with (out_dir / CORPUS_FILE).open("w", encoding="utf-8", newline="\n") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
    with (out_dir / "messages.jsonl").open("w", encoding="utf-8", newline="\n") as fh:
        for m in messages:
            fh.write(
                json.dumps(
                    {
                        "conversation_id": m["conversation_id"],
                        "direction": m["direction"],
                        "author": m["author"],
                        "created_at": m["created_at"].isoformat(),
                        "body": m["body"],
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )
    settings = business.settings or {}
    meta = {
        "taken_at": datetime.now(UTC).isoformat(),
        "business": {
            "id": business.id,
            "slug": business.slug,
            "name": business.name,
            "vertical": business.vertical,
            "default_locale": business.default_locale,
            "settings": {k: settings[k] for k in ("domain_pack", "currency") if k in settings},
        },
        "category_menu": [list(m) for m in menu],
        "vocabulary": vocab_to_doc(vocab),
        "vocabulary_snapshot": snapshot_id(business.domain_pack, vocab),
    }
    (out_dir / VOCAB_FILE).write_text(
        json.dumps(meta, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )
    return {
        "turns": len(rows),
        "conversations": len(conv_ids),
        "messages": len(messages),
        "tool_call_events": sum(len(v.get("tool_call", [])) for v in by_turn.values()),
        "product_search_events": sum(len(v.get("product_search", [])) for v in by_turn.values()),
        "vocabulary_dimensions": len(vocab.dimensions),
        "shelves": len(menu),
    }


# --- lanțul kernelului (funcțiile producției) ----------------------------------------------------


def screen_proposal(recommended: Sequence[Mapping[str, Any]], turn_id: str) -> StateUpdateProposal:
    """EXACT propunerea pe care o emite producția pentru ecranul turului
    (`processor._turn_proposals`): `set_references`, `source="catalog"`, cu ref-urile construite
    de ACEEAȘI funcție (`processor._displayed_product_refs`, recenzia NX-335), deci aruncarea unui
    card fără nume sau preț și conversia prețului sunt ale producției prin construcție. Cu
    `"policy"` setul nou n-ar împinge setul vechi în `recent_sets`."""
    from src.worker.processor import _displayed_product_refs  # noqa: PLC0415

    return StateUpdateProposal(
        "set_references",
        source="catalog",
        turn_id=turn_id,
        payload={"displayed_products": _displayed_product_refs([dict(r) for r in recommended])},
    )


@dataclass(frozen=True)
class KernelTurn:
    """Un tur prin kernel, în afara executorilor: ce a ieșit din fiecare strat pur."""

    validated: Validated | None
    resolved: tuple[ResolvedRef, ...]
    delta: TurnDelta
    gate: GateOutcome | None
    planned: PlannedTurn | None
    state_after: ConversationStateV2


_EMPTY = TurnInterpretation(
    thread="continue",
    acts=[],
    changes=[],
    references=[],
    ambiguities=[],
    corrects_previous_turn=False,
)


async def kernel_turn(
    state: ConversationStateV2,
    interp: TurnInterpretation | None,
    *,
    words: UserWords,
    pack: Any,
    vocab: CatalogVocabulary | None,
    locale: str,
    facts: FactsFn,
    recommended: Sequence[Mapping[str, Any]] = (),
    turn_id: str = "t",
    pairs: Sequence[tuple[str, str]] | None = None,
) -> KernelTurn:
    """Lanțul din `tests/kernel/fixture_catalog.kernel_step`, cu faptele aduse de `facts` (DB
    read-only în rulare, fixture în teste) și ecranul sintetizat din `recommended`. Fără
    `active_search` și fără memoria întrebării: acolo producția ar fi executat planul kernelului, pe
    care replay-ul nu-l execută. O interpretare lipsă (`outcome` ≠ `ok`) rotește doar ecranul."""
    needs = NeedVocabulary.from_pack(pack)
    policy = ReducerPolicy(vocabulary=needs)
    handles = need_handles(state.needs, needs)
    executor = (screen_proposal(recommended, turn_id),) if recommended else ()
    if interp is None:
        delta = to_delta(_EMPTY, [], [], None, handles=handles, needs=needs, turn_id=turn_id)
        after = reduce_turn(state, delta, executor, [], None, False, policy).state
        return KernelTurn(None, (), delta, None, None, after)
    validated = validate(
        interp, words=words, handles=handles, vocab=vocab, pack=pack, locale=locale
    )
    sources = sources_from_state(state, interp.thread)
    lookup = plan_lookup(
        interp.references,
        sources,
        pack=pack,
        locale=locale,
        extra_attributes=lookup_attributes(interp, vocab=vocab, pack=pack),
    )
    known = await facts(lookup)
    resolved = resolve_references(
        interp.references, sources, known, vocab=vocab, pack=pack, locale=locale
    )
    delta = to_delta(
        interp, validated.checked, resolved, known, handles=handles, needs=needs, turn_id=turn_id
    )
    if pairs is not None:
        # NX-348: aceeași verificare a perechii (raft, tip) ca orchestratorul (`mark_pairs`).
        delta = mark_pairs(delta, state, pairs, vocab)
    primary = interp.acts[-1].targets[0] if interp.acts and interp.acts[-1].targets else None
    corrects = interp.corrects_previous_turn
    gate_state = reduce_turn(state, delta, (), resolved, primary, corrects, policy).state
    gate = decide_ambiguity(
        interp,
        validated.checked,
        resolved,
        gate_state,
        known,
        vocab=vocab,
        pack=pack,
        locale=locale,
        policy=GATE_POLICY,
    )
    planned = plan_turn(
        interp,
        gate_state,
        delta.ranking,
        resolved,
        gate,
        changed=bool(delta.proposals),
        pack=pack,
        vocab=vocab,
        locale=locale,
    )
    after = reduce_turn(state, delta, executor, resolved, primary, corrects, policy).state
    return KernelTurn(validated, tuple(resolved), delta, gate, planned, after)


def history_of(previous: Sequence[CorpusTurn]) -> tuple[tuple[str, str], ...]:
    out: list[tuple[str, str]] = []
    for t in previous:
        out.append(("user", t.client_text))
        if t.bot_text:
            out.append(("bot", t.bot_text))
    return tuple(out)


def make_input(snap: Snapshot, state: ConversationStateV2, previous, turn: CorpusTurn):
    return InterpretInput(
        locale=turn.language or snap.business.default_locale,
        pack=snap.pack,
        vocab=snap.vocab,
        category_menu=snap.category_menu,
        state=state,
        history=history_of(previous),
        message=turn.client_text,
    )


# --- etichetele și comparatorul ------------------------------------------------------------------


def load_labels(business: str) -> dict[str, dict[str, Any]]:
    path = LABELS_DIR / f"{business}.json"
    if not path.exists():
        return {}
    return dict(json.loads(path.read_text(encoding="utf-8")).get("turns", {}))


def screen_positions(state: ConversationStateV2) -> dict[str, str]:
    return {d.product_id: f"#{i}" for i, d in enumerate(state.references.displayed_products, 1)}


def observed(
    interp: TurnInterpretation | None,
    kernel: KernelTurn | None,
    state_before: ConversationStateV2,
) -> dict[str, Any]:
    """Interpretarea turului în forma etichetelor: `thread`, actul principal, țintele (poziții de
    pe ecranul de dinainte sau `catalog`), schimbările validate ca `[op, dimensiune, valoare,
    relație]` (valoarea canonică; `null` pe `unmapped`), ambiguitate da/nu."""
    if interp is None:
        return {}
    checked = kernel.validated.checked if kernel and kernel.validated else []
    return observed_parts(interp, checked, kernel.resolved if kernel else (), state_before)


def _umbrella_moves(state: ConversationStateV2, c: Any) -> bool:
    """NX-350 (recenzia, constatarea 9): un tip `implicit` e ipoteză, dar umbrela lui MUTĂ subiectul
    când e alt fel de produs decât cel de acum (un tip spus clar în afara ei, sau o umbrelă
    disjunctă), exact regula reducerului."""
    if c.rejected or c.dimension != "product_type" or c.provenance != "implicit":
        return False
    umbrella = set(getattr(c, "umbrella", ()) or ())
    if not umbrella and isinstance(c.canonical_value, str):
        umbrella = {c.canonical_value}
    topic = state.topic
    stated = None if topic.type_learned else topic.product_type
    if stated is not None:
        return stated not in umbrella
    return bool(topic.type_umbrella) and not umbrella & set(topic.type_umbrella)


def observed_parts(
    interp: TurnInterpretation,
    checked: Sequence[Any],
    resolved: Sequence[ResolvedRef],
    state_before: ConversationStateV2,
    *,
    offscreen_ids: bool = False,
) -> dict[str, Any]:
    """`observed` pe straturile deja calculate. NX-339: aceeași funcție scrie și eticheta unui
    journey (interpretarea AȘTEPTATĂ, trecută prin același validator și același resolver), deci
    eticheta și modelul se compară în aceeași formă canonică.

    `offscreen_ids`: o țintă care nu e pe ecranul curent (parcat, de mai devreme, catalog) se
    numește prin id, nu prin „catalog". Altfel două produse DIFERITE din afara ecranului ar părea
    aceeași țintă (recenzia NX-339, constatarea 2). Doar pe journey-uri, unde eticheta e derivată;
    etichetele SOLE sunt scrise de mână cu „catalog", deci acolo limita rămâne, declarată."""
    positions = screen_positions(state_before)
    by_ref = {r.ref_id: r for r in resolved}
    primary = interp.acts[-1] if interp.acts else None
    targets: list[str] = []
    for t in primary.targets if primary else []:
        ref = by_ref.get(t)
        for pid in ref.product_ids if ref else []:
            targets.append(positions.get(pid, f"id:{pid}" if offscreen_ids else "catalog"))
    changes = []
    provenance: list[str | None] = []
    active: list[bool] = []
    # D3 se judecă pe starea de DINAINTE; un tur care schimbă subiectul sau reia unul parcat
    # retrage/reactivează nevoile, deci acolo nimic nu e „deja activ" (recenzia NX-345a).
    # `clear` și corecția retrag nevoi la fel; un `inferred` nu mută nimic (nu se persistă).
    subject_moves = (
        interp.thread == "resume"
        or bool(interp.corrects_previous_turn)
        or any(not c.rejected and c.change.op == "clear" for c in checked)
        or any(
            not c.rejected
            and not _is_hypothesis(c.provenance, c.dimension)
            and c.dimension in SUBJECT_DIMENSIONS
            and c.canonical_value is not None
            and _moves_subject(state_before, c.dimension, format_value(c.canonical_value))
            for c in checked
        )
        or any(_umbrella_moves(state_before, c) for c in checked)
    )
    for c in checked:
        if c.rejected:
            continue
        value = c.canonical_value
        if c.dimension == "unmapped":
            value = None
        elif value is not None:
            value = format_value(value)
        relation = c.change.relation or "eq"
        changes.append([c.change.op, c.dimension, value, relation])
        provenance.append(c.provenance)
        active.append(
            not subject_moves
            and _OP_CLASS.get(c.change.op) == "assert"
            and _RELATION_CLASS.get(relation) == "positive"
            and already_active(state_before, c.dimension, value, c.provenance)
        )
    return {
        "thread": interp.thread,
        "primary_act": primary.kind if primary else None,
        "targets": sorted(set(targets)),
        "changes": changes,
        # NX-345: aliniate cu `changes`, pentru D1 (ipoteza e neutră) și D3 (un `set` pe valoarea
        # deja activă e neutru). Etichetele le ignoră: comparatorul citește doar `changes`.
        "change_provenance": provenance,
        "change_active": active,
        "ambiguous": bool(interp.ambiguities),
        "corrects": interp.corrects_previous_turn,
    }


def _moves_subject(state: ConversationStateV2, dimension: str, value: str) -> bool:
    """NX-348 (kernel.v3.0): subiectul se mută când o jumătate DEJA SETATĂ primește altă valoare;
    completarea unei jumătăți goale (sau a unui tip dedus) nu îl mută."""
    if dimension == "category":
        current = state.topic.category_key
    else:
        current = None if state.topic.type_learned else state.topic.product_type
    return current is not None and value != current


def already_active(
    state: ConversationStateV2,
    dimension: str,
    value: str | None,
    provenance: str | None = None,
) -> bool:
    """NX-345 D3: valoarea e DEJA activă înaintea turului, iar `set`-ul pe ea nu schimbă nimic în
    stare: raftul subiectului, sau o nevoie activă pe aceeași cheie (delta scrie nevoia pe cheia
    dimensiunii) care e deja `hard` și confirmată. Pe o nevoie `soft`, reducerul NU tratează
    reafirmarea explicită ca nimic: o întărește la `hard` și o confirmă (`_handle_set_need`), deci
    acolo e o schimbare, judecată. Prețul și limitele numerice stau pe chei proprii (`budget_*`,
    `<fațetă>_min/_max`), deci rămân judecate. PUR."""
    if value is None:
        return False
    if dimension == "category":
        return state.topic.category_key == value
    if dimension == "product_type":
        return state.topic.product_type == value and not state.topic.type_learned
    for need in state.active_needs():
        if need.key != dimension or need.normalized_value is None:
            continue
        if format_value(need.normalized_value) != value:
            continue
        if provenance != "explicit" or (need.strength == "hard" and need.confirmed):
            return True
    return False


#: Clasele de op și de relație ale comparatorului (recenzia NX-335): un `avoid` sau un `remove`
#: nu e o potrivire pentru un `add … eq`, dar `set`/`add`/`replace` afirmă toate o valoare, iar
#: `eq`/`contains`/fără relație sunt toate pozitive.
_OP_CLASS = {"set": "assert", "add": "assert", "replace": "assert", "remove": "remove"}
_RELATION_CLASS = {"eq": "positive", "contains": "positive", None: "positive"}


def _key(change: Sequence[Any]) -> tuple[str, str, str | None, str]:
    op, dimension, value, relation = (list(change) + [None] * 4)[:4]
    return (
        _OP_CLASS.get(op, str(op)),
        str(dimension),
        None if value is None else str(value),
        _RELATION_CLASS.get(relation, str(relation)),
    )


def _split(changes: Iterable[Sequence[Any]]) -> tuple[Counter, Counter]:
    """(perechile cu valoare, perechile cu valoare NULĂ: `unmapped` și limitele relative, unde
    valoarea e cuvintele clientului sau un număr calculat de cod, deci nu intră în F1)."""
    valued: Counter = Counter()
    null: Counter = Counter()
    for change in changes:
        key = _key(change)
        if key[2] is None:
            null[(key[0], key[1], key[3])] += 1
        else:
            valued[key] += 1
    return valued, null


#: Câmpurile de acord ale comparatorului.
AGREEMENT_FIELDS: tuple[str, ...] = ("thread", "primary_act", "targets", "ambiguous", "corrects")


def compare(
    label: Mapping[str, Any], got: Mapping[str, Any], *, failed: bool = False
) -> dict[str, Any]:
    """Comparatorul propriu al etichetelor (nu `first_divergence`, care compară straturile unui
    singur trace, pe egalitate exactă): acord pe câmpuri, apoi potriviri pe (clasa op-ului,
    dimensiune, valoare, clasa relației). Perechile cu valoare nulă se numără separat, pe
    (clasa op-ului, dimensiune, clasa relației), în afara F1. `number_for_relative`: eticheta cere
    o limită relativă de preț, iar modelul a pus un număr (riscul prețurilor din HISTORY).

    NX-339: un câmp ABSENT din etichetă nu se evaluează (`None`), unul prezent, chiar gol, da. Un
    journey fixează doar ce testează; etichetele SOLE au toate câmpurile, deci cifrele lor nu se
    schimbă. Un apel EȘUAT (`failed`, fără interpretare) e greșit pe fiecare câmp evaluat: altfel
    o etichetă goală („nicio țintă", „nicio schimbare") ar număra tăcerea ca acord (recenzia
    NX-339, constatarea 3)."""
    evaluated = "changes" in label
    want, want_null = _split(label.get("changes", []) if evaluated else [])
    have, have_null = _split(got.get("changes", []) if evaluated else [])
    relative = any(k[1] == "price" for k in want_null)
    numbered = any(k[1] == "price" for k in have)
    counted = _count_changes(want, want_null, got if evaluated else {})

    def agree(name: str, same: Callable[[Any, Any], bool]) -> bool | None:
        if name not in label:
            return None
        return False if failed else same(label[name], got.get(name))

    return {
        "failed": failed,
        "thread": agree("thread", lambda a, b: a == b),
        "primary_act": agree("primary_act", lambda a, b: a == b),
        "targets": agree("targets", lambda a, b: sorted(a) == sorted(b or [])),
        "ambiguous": agree("ambiguous", lambda a, b: bool(a) == bool(b)),
        "corrects": agree("corrects", lambda a, b: bool(a) == bool(b)),
        "changes_evaluated": evaluated,
        "change_hits": counted["hits"],
        "change_labelled": sum(want.values()),
        "change_emitted": counted["emitted"],
        "null_hits": counted["null_hits"],
        "null_labelled": sum(want_null.values()),
        "null_emitted": counted["null_emitted"],
        # NX-345: ce se numără în plus față de regula de dinainte. `*_all` = toate schimbările
        # emise (regula cu care au fost judecate v1-v3, păstrată ca verdictele lor să rămână
        # reproductibile), `neutral_*` = cele scoase din F1 de D1 / D3.
        "change_emitted_all": sum(have.values()),
        "null_emitted_all": sum(have_null.values()),
        "neutral_hypotheses": counted["neutral_hypotheses"],
        "neutral_active": counted["neutral_active"],
        "hypotheses": counted["hypotheses"],
        "hypotheses_contradicted": counted["hypotheses_contradicted"],
        "hypotheses_redundant": counted["hypotheses_redundant"],
        "provenance_known": counted["provenance_known"],
        # regula de dinainte, pentru `f1_all_emitted` și regresiile pe regula zgomotului
        "change_hits_all": sum((want & have).values()),
        "null_hits_all": sum((want_null & have_null).values()),
        "number_for_relative": relative and numbered,
    }


def _is_hypothesis(provenance: str | None, dimension: str) -> bool:
    """`inferred` nu se persistă pe NICIO dimensiune (I23, `delta`); `implicit` se persistă, iar pe
    raft mută subiectul, deci acolo e fapt."""
    if provenance == "inferred":
        return True
    # NX-350 (kernel.v4.0): un tip `implicit` nu mai devine tipul subiectului (doar umbrela), deci e
    # ipoteză; doar raftul `implicit` rămâne fapt (mută subiectul).
    return provenance in HYPOTHESIS_PROVENANCE and dimension not in IMPLICIT_FACT_DIMENSIONS


def _count_changes(want: Counter, want_null: Counter, got: Mapping[str, Any]) -> dict[str, int]:
    """NX-345 (D1, D3; Adi, 2026-09-29), cu reparațiile recenziei NX-345a. Se numără ce kernelul
    face FAPT:

    - o schimbare `explicit` (sau `implicit` pe raft, care mută subiectul) potrivită cu eticheta e
      un succes, neetichetată e un pozitiv fals;
    - o IPOTEZĂ `implicit` (nevoie `soft`, persistată) potrivită e un succes, neetichetată e neutră;
    - o IPOTEZĂ `inferred` (semnal de ordonare, NEpersistat) nu se potrivește niciodată: dacă
      eticheta o cerea, e un negativ fals (starea n-o poartă), altfel e neutră;
    - un `set` pe valoarea deja activă e neutru (D3).

    Faptele se potrivesc ÎNAINTEA ipotezelor, deci ordinea din interpretare nu mută cifrele. Fără
    proveniență sau fără marcajul de activ (etichete, rapoarte vechi), o schimbare se numără exact
    ca înainte.

    Precizia ipotezelor: o ipoteză nepotrivită e CONTRAZISĂ când eticheta are o schimbare pe
    aceeași (clasă de op, dimensiune), cu sau fără valoare, dar ALTA; cu aceeași valoare e
    REDUNDANTĂ (un `inferred` pe care starea nu-l poartă, sau un duplicat al unui fapt)."""
    changes = list(got.get("changes") or [])
    provenance = list(got.get("change_provenance") or [])
    active = list(got.get("change_active") or [])
    aligned = len(provenance) == len(changes)
    if not aligned:
        provenance = [None] * len(changes)
    if len(active) != len(changes):
        active = [False] * len(changes)
    left, left_null = Counter(want), Counter(want_null)
    labelled_dims = {(k[0], k[1]) for k in want} | {(k[0], k[1]) for k in want_null}
    out = dict.fromkeys(
        (
            "hits",
            "null_hits",
            "emitted",
            "null_emitted",
            "neutral_hypotheses",
            "neutral_active",
            "hypotheses",
            "hypotheses_contradicted",
            "hypotheses_redundant",
        ),
        0,
    )
    out["provenance_known"] = int(aligned and bool(changes))
    items = [
        (change, prov, is_active, _key(change))
        for change, prov, is_active in zip(changes, provenance, active, strict=True)
    ]
    hypothesis_last = sorted(items, key=lambda it: _is_hypothesis(it[1], it[3][1]))
    for _change, prov, is_active, key in hypothesis_last:
        null = key[2] is None
        pool, slot = (left_null, (key[0], key[1], key[3])) if null else (left, key)
        hypothesis = _is_hypothesis(prov, key[1])
        emitted = "null_emitted" if null else "emitted"
        if hypothesis:
            out["hypotheses"] += 1
        if pool[slot] > 0 and prov != "inferred":
            pool[slot] -= 1
            out[emitted] += 1
            out["null_hits" if null else "hits"] += 1
            continue
        if hypothesis:
            out["neutral_hypotheses"] += 1
            if slot in want_null or slot in want:
                # aceeași valoare ca eticheta: un `inferred` (nepersistat) sau un duplicat al unui
                # fapt deja potrivit. Nici contrazisă, nici neetichetată (recenzia NX-345a).
                out["hypotheses_redundant"] += 1
            elif (key[0], key[1]) in labelled_dims:
                out["hypotheses_contradicted"] += 1
        elif is_active:
            out["neutral_active"] += 1
        else:
            out[emitted] += 1
    return out


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float] | None:
    if n <= 0:
        return None
    p = k / n
    den = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / den
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / den
    return (round(max(0.0, centre - half), 3), round(min(1.0, centre + half), 3))


def rate(k: int, n: int) -> dict[str, Any]:
    return {"k": k, "n": n, "rate": round(k / n, 3) if n else None, "wilson95": wilson(k, n)}


def _agrees(verdict: Mapping[str, Any]) -> bool:
    """Acordul pe câmpurile care decid divergența; un câmp neevaluat (`None`) nu o declanșează."""
    return all(verdict[k] is not False for k in ("thread", "primary_act", "targets"))


def raw_interpretation(
    interp: TurnInterpretation | None, validated: Validated | None
) -> dict[str, Any]:
    """NX-339: interpretarea BRUTĂ și verdictul validatorului pe fiecare schimbare, în raportul
    LOCAL (`reports/nx335/`, ignorat de git: conține citatele clientului). Fără ele, o respingere
    `semantic_mismatch` nu se poate explica. Evenimentul `turn_interpretation` rămâne fără text
    (I18); asta e doar unealta de măsurare."""
    return {
        "interpretation": interp.model_dump(mode="json") if interp is not None else None,
        "checked": (
            [c.model_dump(mode="json") for c in validated.checked] if validated is not None else []
        ),
    }


# --- rularea -------------------------------------------------------------------------------------


def state_summary(state: ConversationStateV2) -> dict[str, Any]:
    """Forma stării după tur, doar numere (P12): câte produse pe ecran, câte seturi de mai devreme,
    câte nevoi active, dacă e un subiect parcat."""
    refs = state.references
    return {
        "shown": len(refs.displayed_products),
        "recent_sets": len(refs.recent_sets),
        "needs": len(state.active_needs()),
        "parked": state.parked is not None,
    }


@dataclass
class ArmChain:
    state: ConversationStateV2 = field(default_factory=ConversationStateV2)
    diverged_at: int | None = None


def _v1_search(turn: CorpusTurn) -> dict[str, Any] | None:
    for call in turn.tool_calls:
        if call.get("name") == "search_products":
            return dict(call.get("args") or {})
    return None


def versus_v1(turn: CorpusTurn, kernel: KernelTurn | None) -> dict[str, Any]:
    """Comparația FĂRĂ etichete cu ce a căutat v1 pe același tur: același raft, raftul ghicit de v1
    scos, o nevoie căutată de v1 pe care kernelul n-o mai poartă."""
    v1 = _v1_search(turn)
    plan = None
    if kernel and kernel.planned:
        plan = kernel.planned.plans[kernel.planned.primary]
    args = plan.search_args if plan is not None else None
    if v1 is None or args is None:
        return {"compared": False}
    v1_needs = {str(c) for c in (v1.get("concerns") or []) if c}
    kernel_needs = set(args.concerns or []) | {
        str(v) for values in (args.prefer or {}).values() for v in values
    }
    return {
        "compared": True,
        "same_shelf": (v1.get("category") or None) == (args.category or None),
        "v1_shelf_dropped": bool(v1.get("category")) and not args.category,
        "need_lost": bool(v1_needs - kernel_needs),
    }


async def run(
    snap: Snapshot,
    llm: Any,
    *,
    efforts: Sequence[str],
    facts: FactsFn,
    labels: Mapping[str, Mapping[str, Any]],
    seed: int,
    dry_run: bool,
    limit: int | None = None,
    pairs: Sequence[tuple[str, str]] | None = None,
) -> list[dict[str, Any]]:
    """Un rând de raport per tur și braț. `dry_run` ⇒ ZERO apeluri: se construiește doar promptul
    (cu ecranul rotit din `recommended`), pentru tokeni și cost."""
    rng = random.Random(seed)
    rows: list[dict[str, Any]] = []
    for conversation in snap.conversations()[: limit or None]:
        chains = {arm: ArmChain() for arm in efforts}
        for index, turn in enumerate(conversation):
            order = list(efforts)
            rng.shuffle(order)
            for arm in order:
                chain = chains[arm]
                inp = make_input(snap, chain.state, conversation[:index], turn)
                row: dict[str, Any] = {
                    "turn_id": turn.turn_id,
                    "conversation_id": turn.conversation_id,
                    "seq": turn.seq,
                    "arm": arm,
                    "order": order,
                    "system_chars": len(system_prompt(inp)),
                    "user_chars": len(user_prompt(inp)),
                }
                if dry_run:
                    kernel = await kernel_turn(
                        chain.state,
                        None,
                        words=user_words(inp),
                        pack=snap.pack,
                        vocab=snap.vocab,
                        locale=inp.locale,
                        facts=facts,
                        recommended=turn.recommended,
                        pairs=pairs,
                        turn_id=turn.turn_id,
                    )
                    chain.state = kernel.state_after
                    row["state"] = state_summary(chain.state)
                    rows.append(row)
                    continue
                acc, token = usage.push()
                try:
                    out: InterpretedTurn = await interpret_turn(
                        llm, inp, business_id=snap.business.id, effort=arm
                    )
                finally:
                    usage.pop(token)
                before = chain.state
                kernel = await kernel_turn(
                    before,
                    out.interpretation,
                    words=user_words(inp),
                    pack=snap.pack,
                    vocab=snap.vocab,
                    locale=inp.locale,
                    facts=facts,
                    recommended=turn.recommended,
                    pairs=pairs,
                    turn_id=turn.turn_id,
                )
                chain.state = kernel.state_after
                got = observed(out.interpretation, kernel, before)
                label = labels.get(turn.turn_id)
                verdict = None
                if label and not label.get("uncertain"):
                    verdict = compare(label, got, failed=out.interpretation is None)
                    if not _agrees(verdict) and chain.diverged_at is None:
                        chain.diverged_at = turn.seq
                call = acc.call_rows[-1] if acc.call_rows else {}
                row.update(
                    {
                        "outcome": out.outcome,
                        "event": out.event,
                        **raw_interpretation(out.interpretation, kernel.validated),
                        "observed": got,
                        "verdict": verdict,
                        "first_divergence": chain.diverged_at,
                        "ms": call.get("ms"),
                        "tokens_in": call.get("tokens_in"),
                        "cached": call.get("cached"),
                        "tokens_out": call.get("tokens_out"),
                        "cost_usd": round(acc.cost_usd, 6),
                        "versus_v1": versus_v1(turn, kernel),
                        "state": state_summary(chain.state),
                    }
                )
                rows.append(row)
    return rows


# --- journey-urile kernelului: setul NEVĂZUT (NX-339) ---------------------------------------------

JOURNEYS_DIR = ROOT / "tests" / "golden" / "kernel_journeys"
#: NX-339c: setul C, scris de un agent independent care n-a văzut promptul, rezultatele sau
#: seturile A și B, pentru verdictul lui `interpret.v3`. ÎNGHEȚAT înaintea primei rulări: amprenta
#: e pe bytes normalizați CRLF→LF, iar testul pică la orice etichetă schimbată.
HOLDOUT_C_DIR = ROOT / "tests" / "golden" / "kernel_interpret_holdout"
HOLDOUT_C_SHA256 = "72fd676ef6dd092464021728cb31d0b535cc9687c4358e1edc47580a64f9ba91"


#: NX-347: setul D, scris de un agent independent care n-a văzut promptul, rezultatele, seturile
#: A, B, C sau cardurile NX-339/345/347, pentru verdictul lui `interpret.v4`. ÎNGHEȚAT la scriere
#: (amprenta raportată de agent), iar autorul promptului nu i-a citit conținutul.
HOLDOUT_D_DIR = ROOT / "tests" / "golden" / "kernel_interpret_holdout_d"
HOLDOUT_D_SHA256 = "68c58e79a750e7f60b63055377422973a6b3fcf0e21597c697d788bd77571ac9"
#: NX-350: setul E, scris de un agent independent care n-a văzut promptul, rapoartele, seturile
#: A-D sau cardurile NX-339/345/347-350, pentru verdictul regulii `kernel.v4.0`. ÎNGHEȚAT.
HOLDOUT_E_DIR = ROOT / "tests" / "golden" / "kernel_interpret_holdout_e"
HOLDOUT_E_SHA256 = "6532611feb7cfd6c3ca11b8997f469a3f1d252aed877874a6a877d51b5cc425f"


def holdout_c_digest(directory: Path = HOLDOUT_C_DIR) -> str:
    """SHA-256 peste fișierele unui set nevăzut (C sau D), sortate, cu CRLF→LF."""
    digest = hashlib.sha256()
    for path in sorted(directory.glob("*.json")):
        digest.update(path.read_bytes().replace(b"\r\n", b"\n"))
    return digest.hexdigest()


#: Pachetele journey-urilor care NU intră în setul nevăzut: `sole-ro` e tenantul pe care promptul se
#: reglează, deci turele lui se raportează separat (A-bis).
TUNED_PACKS: frozenset[str] = frozenset({"sole-ro"})
#: Câmpul etichetei ← cheia din `expect.interpretation` care îl fixează.
_LABEL_FIELDS: dict[str, str] = {
    "thread": "thread",
    "primary_act": "acts",
    "targets": "references",
    "changes": "changes",
    "ambiguous": "ambiguities",
    "corrects": "corrects_previous_turn",
}


@dataclass(frozen=True)
class JourneyCase:
    """Un tur de journey, gata de interpretat: intrarea adaptorului pe starea din `chain` (derivată
    din interpretările AȘTEPTATE ale turelor de dinainte, deci o greșeală nu se propagă) și eticheta
    în forma comparatorului."""

    journey_id: str
    pack: str
    index: int
    set_name: str
    inp: InterpretInput
    earlier: tuple[str, ...]
    state_before: ConversationStateV2
    shown: tuple[str, ...]
    label: dict[str, Any]

    @property
    def turn_id(self) -> str:
        return f"{self.journey_id}#{self.index}"


def journey_fields(directory: Path = JOURNEYS_DIR) -> dict[tuple[str, int], frozenset[str]]:
    """Cheile pe care fiecare tur le fixează în `expect.interpretation`, din JSON-ul BRUT:
    `replay.load_journeys` extinde eticheta compactă la forma completă, deci acolo un `changes`
    absent devine `[]` și distincția „neevaluat" / „gol" s-ar pierde."""
    out: dict[tuple[str, int], frozenset[str]] = {}
    for path in sorted(directory.glob("*.json")):
        for journey in json.loads(path.read_text(encoding="utf-8"))["journeys"]:
            for index, turn in enumerate(journey["turns"]):
                raw = (turn.get("expect") or {}).get("interpretation")
                if raw is not None:
                    out[(journey["journey_id"], index)] = frozenset(raw)
    return out


def injected_state(journey: Any) -> bool:
    """Journey-ul pornește dintr-o stare SCRISĂ de mână (`sources`: ecran, seturi de mai devreme,
    parcat, pagină, focus; `state_before`: handle-uri, subiect), nu din turele lui. Sunt fixture-uri
    pentru UN strat (resolverul, secțiunea C), iar `chain` pornește mereu dintr-o stare goală:
    turul ar fi pus în fața modelului pe un ecran gol, iar eticheta s-ar prăbuși la fel (recenzia
    NX-339, constatarea 1). Pagina și focusul nici nu sunt stare a conversației, deci nu se pot
    reconstrui."""
    return any(t.sources or t.state_before for t in journey.turns)


def journey_cases(
    directory: Path = JOURNEYS_DIR,
    *,
    excluded: Counter | None = None,
    set_name: str | None = None,
) -> list[JourneyCase]:
    """Turele cu interpretare așteptată, pe catalogul de fixture al pachetului lor
    (`stage_harness.catalog`: pachet, vocabular, meniu, fapte), fără DB și fără model. Journey-urile
    cu stare injectată se sar; `excluded` numără turele sărite, pe set, ca să fie raportate.

    `set_name` fixează numele setului pentru TOATE turele (un set nevăzut scris separat, ca setul C
    al NX-339, pe care niciun pachet nu e „reglat"); implicit, împărțirea B / A-bis."""

    def name_of(pack: str) -> str:
        if set_name is not None:
            return set_name
        return "A-bis" if pack in TUNED_PACKS else "B"

    from tests.kernel import replay  # noqa: PLC0415 — precedent: kernel_plan_snapshot.py
    from tests.kernel import stage_harness as sh  # noqa: PLC0415

    fields = journey_fields(directory)
    catalogs: dict[str, Any] = {}
    cases: list[JourneyCase] = []
    for journey in replay.load_journeys(directory):
        if injected_state(journey):
            if excluded is not None:
                excluded[name_of(journey.pack)] += sum(
                    1 for i in range(len(journey.turns)) if (journey.journey_id, i) in fields
                )
            continue
        cat = catalogs.setdefault(journey.pack, sh.catalog(journey.pack))
        for ct in sh.chain(journey, cat):
            keys = fields.get((journey.journey_id, ct.index))
            if keys is None:
                continue
            full = observed_parts(
                ct.step.interpretation,
                ct.step.checked,
                ct.step.resolved,
                ct.state_before,
                offscreen_ids=True,
            )
            label = {k: full[k] for k, source in _LABEL_FIELDS.items() if source in keys}
            inp = InterpretInput(
                locale=journey.locale,
                pack=cat.pack,
                vocab=cat.vocab,
                category_menu=cat.menu,
                state=ct.state_before,
                history=tuple(("user", text) for text in ct.previous),
                message=ct.turn.user_input,
            )
            cases.append(
                JourneyCase(
                    journey_id=journey.journey_id,
                    pack=journey.pack,
                    index=ct.index,
                    set_name=name_of(journey.pack),
                    inp=inp,
                    # EXACT cuvintele cu care `chain` a validat eticheta (toate turele de
                    # dinainte, recent întâi), nu fereastra de 8 a lui `user_words`: același
                    # validator, aceeași intrare, pe ambele părți ale comparației.
                    earlier=ct.previous[::-1],
                    state_before=ct.state_before,
                    shown=ct.turn.shown,
                    label=label,
                )
            )
    return cases


async def run_journeys(
    cases: Sequence[JourneyCase],
    llm: Any,
    *,
    efforts: Sequence[str],
    seed: int,
    dry_run: bool,
) -> list[dict[str, Any]]:
    """Un rând per tur și braț, pe journey-uri. Interpretarea modelului trece prin ACELAȘI pas
    pur ca eticheta (`fixture_catalog.kernel_step`, pe starea din `chain`), deci diferența dintre
    ele e a modelului, nu a căii de comparare. `dry_run` ⇒ ZERO apeluri."""
    from tests.kernel import fixture_catalog  # noqa: PLC0415
    from tests.kernel import stage_harness as sh  # noqa: PLC0415

    rng = random.Random(seed)
    catalogs: dict[str, Any] = {}
    rows: list[dict[str, Any]] = []
    for case in cases:
        order = list(efforts)
        rng.shuffle(order)
        for arm in order:
            row: dict[str, Any] = {
                "turn_id": case.turn_id,
                "journey_id": case.journey_id,
                "pack": case.pack,
                "set": case.set_name,
                "arm": arm,
                "order": order,
                "system_chars": len(system_prompt(case.inp)),
                "user_chars": len(user_prompt(case.inp)),
                "first_divergence": None,
            }
            if dry_run:
                rows.append(row)
                continue
            acc, token = usage.push()
            try:
                out: InterpretedTurn = await interpret_turn(
                    llm, case.inp, business_id=f"b-{case.pack}", effort=arm
                )
            finally:
                usage.pop(token)
            got: dict[str, Any] = {}
            checked: list[Any] = []
            if out.interpretation is not None:
                cat = catalogs.setdefault(case.pack, sh.catalog(case.pack))
                step = fixture_catalog.kernel_step(
                    case.pack,
                    case.state_before,
                    out.interpretation,
                    case.inp.message,
                    earlier=case.earlier,
                    shown_ids=case.shown,
                    turn_id=f"t{case.index}",
                    locale=case.inp.locale,
                    loaded=cat.pack,
                    vocab=cat.vocab,
                    catalog=cat.facts,
                    answer_pending=True,
                )
                checked = list(step.checked)
                got = observed_parts(
                    out.interpretation,
                    step.checked,
                    step.resolved,
                    case.state_before,
                    offscreen_ids=True,
                )
            call = acc.call_rows[-1] if acc.call_rows else {}
            row.update(
                {
                    "outcome": out.outcome,
                    "event": out.event,
                    "interpretation": (
                        out.interpretation.model_dump(mode="json")
                        if out.interpretation is not None
                        else None
                    ),
                    "checked": [c.model_dump(mode="json") for c in checked],
                    "observed": got,
                    "verdict": compare(case.label, got, failed=out.interpretation is None),
                    "ms": call.get("ms"),
                    "tokens_in": call.get("tokens_in"),
                    "cached": call.get("cached"),
                    "tokens_out": call.get("tokens_out"),
                    "cost_usd": round(acc.cost_usd, 6),
                }
            )
            rows.append(row)
    return rows


def summarize_sets(rows: Sequence[Mapping[str, Any]], efforts: Sequence[str]) -> dict[str, Any]:
    """`summarize` per set (B = nevăzut, A-bis = journey-urile tenantului reglat)."""
    out: dict[str, Any] = {}
    for name in sorted({r["set"] for r in rows}):
        mine = [r for r in rows if r["set"] == name]
        out[name] = summarize(mine, efforts)["arms"]
    return {"thresholds": THRESHOLDS, "sets": out}


# --- regresiile tur cu tur între două rapoarte (NX-339) -------------------------------------------


def _exact_changes(verdict: Mapping[str, Any], rule: str = "nx345") -> bool | None:
    """Schimbările turului sunt exact eticheta. `rule="all"` = regula de dinainte de NX-345 (toate
    schimbările emise), cea pe care a fost măsurat zgomotul v1 → v1'."""
    if not verdict.get("changes_evaluated", True):
        return None
    if verdict.get("failed"):
        return False
    suffix = "_all" if rule == "all" else ""

    def get(name: str) -> int:
        return verdict.get(name + suffix, verdict[name])

    return get("change_hits") == verdict["change_labelled"] == get("change_emitted") and get(
        "null_hits"
    ) == verdict["null_labelled"] == get("null_emitted")


def current_labels(rows: Sequence[Mapping[str, Any]], business: str) -> dict[str, dict[str, Any]]:
    """Etichetele de ACUM pentru rândurile unui raport: ale journey-urilor (derivate) pentru
    rândurile cu `set`, ale tenantului (fără `uncertain`) pentru rest."""
    labels: dict[str, dict[str, Any]] = {}
    if any("set" in r for r in rows):
        labels.update({c.turn_id: c.label for c in journey_cases()})
    if any("set" not in r for r in rows):
        labels.update({k: v for k, v in load_labels(business).items() if not v.get("uncertain")})
    return labels


def rescore(
    rows: Sequence[Mapping[str, Any]], labels: Mapping[str, Mapping[str, Any]]
) -> list[dict[str, Any]]:
    """Verdictele RECALCULATE pe etichetele de acum, din `observed` salvat în rând. Protocolul
    NX-339 permite corectarea etichetelor între rularea v1 și v2; fără recalcul, o etichetă
    schimbată ar arăta ca o schimbare a modelului (recenzia NX-339, constatarea 4)."""
    out: list[dict[str, Any]] = []
    for r in rows:
        label = labels.get(r["turn_id"])
        verdict = None
        if label is not None and "outcome" in r:
            got = with_provenance(r.get("observed") or {}, r.get("checked") or [])
            verdict = compare(label, got, failed=r["outcome"] != "ok")
        out.append({**r, "verdict": verdict})
    return out


def with_provenance(got: Mapping[str, Any], checked: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """NX-345: un raport de dinainte de D1 n-are `change_provenance` în `observed`, dar păstrează
    `checked` (verdictul validatorului). `changes` e construit din `checked` fără cele respinse, în
    ordine, deci proveniența se reface pe poziție. D3 NU se poate reface (rândul păstrează doar
    rezumatul numeric al stării): pe un raport vechi `set`-ul pe valoarea activă rămâne judecat."""
    if "change_provenance" in got:
        return dict(got)
    kept = [c for c in checked if isinstance(c, Mapping) and not c.get("rejected")]
    if len(kept) != len(got.get("changes") or []):
        return dict(got)
    return {**got, "change_provenance": [c.get("provenance") for c in kept]}


def regressions(
    before: Sequence[Mapping[str, Any]],
    after: Sequence[Mapping[str, Any]],
    *,
    rule: str = "nx345",
) -> dict[str, Any]:
    """Turele care trec din CORECT în GREȘIT (și invers) între două rapoarte, pe fiecare câmp
    evaluat și pe schimbări (potrivire exactă). Se compară doar INTERSECȚIA (tur, braț), iar
    turele dintr-un singur raport se numără, nu se ascund."""

    def index(rows: Sequence[Mapping[str, Any]]) -> dict[tuple[str, str], Mapping[str, Any]]:
        return {(r["turn_id"], r["arm"]): r["verdict"] for r in rows if r.get("verdict")}

    a, b = index(before), index(after)
    common = sorted(set(a) & set(b))
    worse: list[dict[str, Any]] = []
    better = 0
    for key in common:
        lost: list[str] = []
        gained: list[str] = []
        for name in AGREEMENT_FIELDS:
            x, y = a[key].get(name), b[key].get(name)
            if x is True and y is False:
                lost.append(name)
            elif x is False and y is True:
                gained.append(name)
        x, y = _exact_changes(a[key], rule), _exact_changes(b[key], rule)
        if x is True and y is False:
            lost.append("changes")
        elif x is False and y is True:
            gained.append("changes")
        if lost:
            worse.append({"turn_id": key[0], "arm": key[1], "fields": lost})
        better += bool(gained)
    return {
        "compared": len(common),
        "only_before": len(set(a) - set(b)),
        "only_after": len(set(b) - set(a)),
        "regressed": len(worse),
        "improved": better,
        "turns": worse,
    }


def _field_rate(scored: Sequence[Mapping[str, Any]], name: str) -> dict[str, Any]:
    """Rata de acord pe un câmp, doar pe turele unde câmpul a fost EVALUAT (NX-339)."""
    values = [v[name] for v in scored if v.get(name) is not None]
    return rate(sum(values), len(values))


def _pct(values: Sequence[float], q: float) -> float | None:
    xs = sorted(v for v in values if v is not None)
    return round(xs[min(len(xs) - 1, int(q * len(xs)))], 1) if xs else None


def _round(x: float | None) -> float | None:
    return round(x, 3) if x is not None else None


def _prf(hits: int, labelled: int, emitted: int) -> tuple[float | None, ...]:
    precision = hits / emitted if emitted else None
    recall = hits / labelled if labelled else None
    f1 = (
        2 * precision * recall / (precision + recall)
        if precision and recall
        else (0.0 if precision == 0 or recall == 0 else None)
    )
    return precision, recall, f1


def summarize(rows: Sequence[Mapping[str, Any]], efforts: Sequence[str]) -> dict[str, Any]:
    """Per braț: acordul pe actul principal, `thread`, ținte (cu `n` și interval Wilson), precizia
    și recall-ul schimbărilor, proveniența, respingerile, `unmapped`, `unknown_reference`,
    `outcome` ≠ `ok`, latența și costul, comparația cu v1. Doar numere (P12)."""
    out: dict[str, Any] = {}
    for arm in efforts:
        mine = [r for r in rows if r["arm"] == arm and "outcome" in r]
        scored = [r["verdict"] for r in mine if r.get("verdict")]
        with_changes = [v for v in scored if v.get("changes_evaluated", True)]
        hits = sum(v["change_hits"] for v in with_changes)
        labelled = sum(v["change_labelled"] for v in with_changes)
        emitted = sum(v["change_emitted"] for v in with_changes)
        precision, recall, f1 = _prf(hits, labelled, emitted)
        emitted_all = sum(v.get("change_emitted_all", v["change_emitted"]) for v in with_changes)
        hits_all = sum(v.get("change_hits_all", v["change_hits"]) for v in with_changes)
        hypotheses = sum(v.get("hypotheses", 0) for v in with_changes)
        contradicted = sum(v.get("hypotheses_contradicted", 0) for v in with_changes)
        provenance: Counter[str] = Counter()
        rejected: Counter[str] = Counter()
        unmapped = changes = unknown = 0
        for r in mine:
            ev = r["event"]
            provenance.update(ev["provenance"])
            rejected.update({k: v for k, v in ev["rejected"].items() if v})
            # NX-339: doar pe turele reușite; un apel eșuat nu declară referințe, deci ar fi
            # numărat ca „zero necunoscute" și ar fi trecut pragul (`not_ok` e poarta lui).
            if r["outcome"] == "ok":
                unknown += int(ev["unknown_reference"] > 0)
            for c in (r.get("observed") or {}).get("changes", []):
                changes += 1
                unmapped += int(c[1] == "unmapped")
        vs = [r["versus_v1"] for r in mine if r.get("versus_v1", {}).get("compared")]
        outcomes = Counter(r["outcome"] for r in mine)
        out[arm] = {
            "turns": len(mine),
            "outcomes": dict(outcomes),
            "not_ok": rate(len(mine) - outcomes.get("ok", 0), len(mine)),
            "primary_act": _field_rate(scored, "primary_act"),
            "thread": _field_rate(scored, "thread"),
            "targets": _field_rate(scored, "targets"),
            "ambiguity": _field_rate(scored, "ambiguous"),
            "corrects": _field_rate(scored, "corrects"),
            "changes_turns": len(with_changes),
            "changes": {
                "precision": round(precision, 3) if precision is not None else None,
                "recall": round(recall, 3) if recall is not None else None,
                "f1": round(f1, 3) if f1 is not None else None,
                "labelled": labelled,
                "emitted": emitted,
                # NX-345: F1 pe regula de dinainte (toate schimbările emise), ca v1-v3 să rămână
                # judecate pe regula cu care au fost rulate.
                "f1_all_emitted": _round(_prf(hits_all, labelled, emitted_all)[2]),
                "emitted_all": emitted_all,
                "neutral_hypotheses": sum(v.get("neutral_hypotheses", 0) for v in with_changes),
                "neutral_active": sum(v.get("neutral_active", 0) for v in with_changes),
            },
            "hypotheses": {**rate(contradicted, hypotheses), "contradicted": contradicted},
            # NX-345a: ipotezele NEETICHETATE (neutre în F1), pe tur: paza împotriva unui prompt
            # care ar scăpa de pozitivele false coborând proveniența (recenzia NX-345a).
            "unlabelled_hypotheses": rate(
                sum(
                    v.get("neutral_hypotheses", 0) - v.get("hypotheses_redundant", 0)
                    for v in with_changes
                ),
                len(with_changes),
            ),
            "provenance_unknown_turns": sum(
                1
                for v in with_changes
                if v.get("change_emitted_all") and not v.get("provenance_known")
            ),
            "null_valued_changes": {
                "hits": sum(v["null_hits"] for v in with_changes),
                "labelled": sum(v["null_labelled"] for v in with_changes),
                "emitted": sum(v["null_emitted"] for v in with_changes),
            },
            "number_for_relative": sum(v["number_for_relative"] for v in with_changes),
            "provenance": dict(provenance),
            "rejected": dict(rejected),
            "unmapped": rate(unmapped, changes),
            "unknown_reference": rate(unknown, sum(1 for r in mine if r["outcome"] == "ok")),
            "ms_p50": _pct([r["ms"] for r in mine], 0.5),
            "ms_p90": _pct([r["ms"] for r in mine], 0.9),
            "cost_usd_total": round(sum(r["cost_usd"] for r in mine), 4),
            "cost_usd_per_turn": round(sum(r["cost_usd"] for r in mine) / len(mine), 6)
            if mine
            else None,
            "versus_v1": {
                "compared": len(vs),
                "same_shelf": sum(v["same_shelf"] for v in vs),
                "v1_shelf_dropped": sum(v["v1_shelf_dropped"] for v in vs),
                "need_lost": sum(v["need_lost"] for v in vs),
            },
            "first_divergence": {
                r["conversation_id"]: r["first_divergence"]
                for r in mine
                if r["first_divergence"] is not None
            },
        }
    return {"thresholds": THRESHOLDS, "arms": out}


def estimate(rows: Sequence[Mapping[str, Any]], model: str) -> dict[str, Any]:
    """Tokenii și costul ESTIMATE ale unei rulări (≈ 4 caractere pe token, ieșire ~400)."""
    from src.agent.pricing import cost_for  # noqa: PLC0415

    ins = [(r["system_chars"] + r["user_chars"]) // CHARS_PER_TOKEN for r in rows]
    cached = [r["system_chars"] // CHARS_PER_TOKEN for r in rows]
    total = sum(cost_for(model, i, 0, EST_OUTPUT_TOKENS) for i in ins)
    warm = sum(cost_for(model, i, c, EST_OUTPUT_TOKENS) for i, c in zip(ins, cached, strict=True))
    return {
        "calls": len(rows),
        "tokens_in_p50": _pct([float(i) for i in ins], 0.5),
        "tokens_in_max": max(ins) if ins else 0,
        "system_tokens": cached[0] if cached else 0,
        "cost_usd_no_cache": round(total, 4),
        "cost_usd_cached_prefix": round(warm, 4),
    }


class _TenantDeps:
    """`deps.db(op)` = checkout tenant-scoped, ca `fetch_reference_facts` să citească exact ca
    producția (un checkout, `business_id = $1`)."""

    def __init__(self, business_id: str):
        self._bid = business_id

    def db(self, _op: str):
        from src.db.connection import tenant_conn  # noqa: PLC0415

        return tenant_conn(self._bid)


def _parse_efforts(raw: str) -> tuple[str, ...]:
    out = tuple(e.strip() for e in raw.split(",") if e.strip())
    if not out or len(set(out)) != len(out) or set(out) - set(INTERPRET_EFFORTS):
        raise SystemExit(f"--efforts: valori distincte din {INTERPRET_EFFORTS}, nu {raw!r}")
    return out


async def _empty_facts(_lookup: CatalogLookup) -> ReferenceFacts:
    return ReferenceFacts()


async def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--business", default="sole-ro")
    ap.add_argument(
        "--snapshot", action="store_true", help="instantaneul (DB read-only, 0 apeluri)"
    )
    ap.add_argument("--dir", type=Path, default=None, help="directorul instantaneului")
    ap.add_argument("--efforts", default="none")
    ap.add_argument("--seed", type=int, default=335)
    ap.add_argument("--limit", type=int, default=None, help="câte conversații")
    ap.add_argument("--dry-run", action="store_true", help="implicit; zero apeluri de model")
    ap.add_argument("--yes", action="store_true", help="confirmă că rularea consumă credite")
    ap.add_argument(
        "--journeys",
        action="store_true",
        help="NX-339: journey-urile kernelului (setul nevăzut B + A-bis), fără DB",
    )
    ap.add_argument(
        "--journeys-dir",
        type=Path,
        default=None,
        help="NX-339: alt director de journey-uri (setul C), cu --journeys",
    )
    ap.add_argument(
        "--set-name",
        default=None,
        help="NX-339: numele setului pentru toate turele din --journeys-dir (ex. C)",
    )
    ap.add_argument(
        "--regressions",
        nargs=2,
        type=Path,
        metavar=("INAINTE", "DUPA"),
        help="NX-339: turele care trec din corect în greșit între două results.json (0 apeluri)",
    )
    args = ap.parse_args(argv)
    directory = args.dir or (OUT_DIR / args.business)

    if args.regressions:
        before, after = (
            json.loads(p.read_text(encoding="utf-8"))["rows"] for p in args.regressions
        )
        labels = current_labels([*before, *after], args.business)
        a, b = rescore(before, labels), rescore(after, labels)
        # NX-345a: poarta de zgomot (NX-339) se judecă pe regula pe care a fost măsurat zgomotul
        # (`all`); regula nouă se raportează alături.
        report = {**regressions(a, b), "all_emitted": regressions(a, b, rule="all")}
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0

    if args.journeys:
        return await _main_journeys(args)

    if args.snapshot:
        counts = await take_snapshot(args.business, directory)
        print(json.dumps(counts, indent=2))
        print(f"instantaneu: {directory} (LOCAL, nu intră în repo: conține texte de client)")
        return 0

    efforts = _parse_efforts(args.efforts)
    missing = [n for n in (CORPUS_FILE, VOCAB_FILE) if not (directory / n).exists()]
    if missing:
        # NX-339: instantaneul e LOCAL (texte de client, ignorat de git), deci pe altă mașină sau
        # într-un worktree nou lipsește; un traceback ar ascunde de ce.
        print(
            f"lipsește instantaneul local în {directory} ({', '.join(missing)}); "
            f"rulează întâi --snapshot (DB read-only, zero apeluri)",
            file=sys.stderr,
        )
        return 2
    snap = load_snapshot(directory)
    labels = load_labels(args.business)
    live = args.yes and not args.dry_run
    llm = None
    facts: FactsFn = _empty_facts
    pairs: list[tuple[str, str]] | None = None
    if live:
        from src.agent.llm import get_llm  # noqa: PLC0415
        from src.catalog.reference_facts import fetch_reference_facts  # noqa: PLC0415

        llm = get_llm()
        if llm is None:
            raise SystemExit("lipsește OPENAI_API_KEY")
        deps = _TenantDeps(snap.business.id)

        async def facts(lookup: CatalogLookup) -> ReferenceFacts:
            return await fetch_reference_facts(deps, snap.business.id, lookup)

        # NX-348: perechile (raft, tip) ale catalogului, o singură citire, ca în producție.
        async with deps.db("kernel_subject_pairs") as conn:
            pairs = await subject_type_pairs(conn, snap.business.id)

    try:
        rows = await run(
            snap,
            llm,
            efforts=efforts,
            facts=facts,
            labels=labels,
            seed=args.seed,
            dry_run=not live,
            limit=args.limit,
            pairs=pairs,
        )
    finally:
        if live:
            from src.db.connection import close_pool  # noqa: PLC0415

            await close_pool()

    from src.config import get_settings  # noqa: PLC0415

    model = get_settings().model_agent
    scored = sum(1 for t in snap.turns if t.turn_id in labels)
    uncertain = sum(1 for t in snap.turns if labels.get(t.turn_id, {}).get("uncertain"))
    print(
        f"ture: {len(snap.turns)}  conversații: {len(snap.conversations())}  "
        f"etichetate: {scored} (nesigure: {uncertain})  brațe: {efforts}  apeluri: {len(rows)}"
    )
    print(f"vocabulary_snapshot (instantaneu): {snap.vocabulary_snapshot}")
    print(json.dumps(estimate(rows, model), indent=2))
    if not live:
        if not args.dry_run:
            print("Rularea reală consumă credite OpenAI. Repornește cu --yes (o pornește Adi).")
        return 0

    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    out_dir = directory / f"run-{stamp}"
    out_dir.mkdir(parents=True, exist_ok=True)
    summary = summarize(rows, efforts)
    (out_dir / "results.json").write_text(
        json.dumps(
            {"model": model, "efforts": list(efforts), "summary": summary, "rows": rows},
            ensure_ascii=False,
            indent=2,
            default=str,
        ),
        encoding="utf-8",
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print(f"\nraport: {out_dir}")
    return 0


async def _main_journeys(args: argparse.Namespace) -> int:
    """`--journeys`: setul nevăzut (NX-339). Fără DB: catalogul, vocabularul și faptele sunt ale
    fixture-ului. Raportul tipărește DOAR agregatele; rândurile stau în `results.json` (local),
    iar protocolul cere să nu fie deschise înainte de înghețarea promptului."""
    from src.config import get_settings  # noqa: PLC0415

    efforts = _parse_efforts(args.efforts)
    excluded: Counter = Counter()
    directory = args.journeys_dir or JOURNEYS_DIR
    frozen = {
        HOLDOUT_C_DIR.resolve(): HOLDOUT_C_SHA256,
        HOLDOUT_D_DIR.resolve(): HOLDOUT_D_SHA256,
        HOLDOUT_E_DIR.resolve(): HOLDOUT_E_SHA256,
    }
    expected = frozen.get(Path(directory).resolve())
    if args.yes and expected is not None and holdout_c_digest(directory) != expected:
        # NX-347: un set nevăzut schimbat după îngheț nu mai judecă nimic; fail-closed ÎNAINTEA
        # oricărui apel de model (recenzia v4).
        print(f"refuz: setul înghețat din {directory} nu mai are amprenta din cod")
        return 2
    set_name = args.set_name or ("C" if args.journeys_dir else None)
    cases = journey_cases(directory, excluded=excluded, set_name=set_name)
    live = args.yes and not args.dry_run
    llm = None
    if live:
        from src.agent.llm import get_llm  # noqa: PLC0415

        llm = get_llm()
        if llm is None:
            raise SystemExit("lipsește OPENAI_API_KEY")
    rows = await run_journeys(cases, llm, efforts=efforts, seed=args.seed, dry_run=not live)
    per_set = Counter(c.set_name for c in cases)
    per_pack = Counter(c.pack for c in cases)
    print(
        f"journey-uri: ture {len(cases)}  seturi {dict(sorted(per_set.items()))}  "
        f"pachete {dict(sorted(per_pack.items()))}  brațe: {efforts}  apeluri: {len(rows)}"
    )
    print(f"excluse (stare injectată, NX-339): {dict(sorted(excluded.items()))}")
    model = get_settings().model_agent
    print(json.dumps(estimate(rows, model), indent=2))
    if not live:
        if not args.dry_run:
            print("Rularea reală consumă credite OpenAI. Repornește cu --yes (o pornește Adi).")
        return 0
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    folder = "journeys" if set_name is None else f"journeys-{set_name.lower()}"
    out_dir = OUT_DIR / folder / f"run-{stamp}"
    out_dir.mkdir(parents=True, exist_ok=True)
    summary = {**summarize_sets(rows, efforts), "excluded_injected_state": dict(excluded)}
    (out_dir / "results.json").write_text(
        json.dumps(
            {"model": model, "efforts": list(efforts), "summary": summary, "rows": rows},
            ensure_ascii=False,
            indent=2,
            default=str,
        ),
        encoding="utf-8",
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print(f"\nraport: {out_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
