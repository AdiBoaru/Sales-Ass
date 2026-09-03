"""NX-241 — admission, paralelism și plafoane de rezultat pentru tool-urile agentului.

Trei lucruri pe care bucla de tool-calling nu le avea:

  1. **Clasificare.** Fiecare tool e read-only sau MUTAȚIE, paralel-safe sau nu, idempotent sau nu,
     critic sau opțional. Fără asta, „rulăm tool-urile concurent ca să tăiem latența" (ce face azi
     `run_tool_loop` cu `asyncio.gather`) e o promisiune că nicio mutație nu se lansează speculativ
     — promisiune pe care n-o verifică nimeni. Registrul e COMPLET prin construcție: un tool nou
     fără clasificare crapă la import, nu la prima furtună de tool calls în producție.
  2. **Admission.** Un tool call se rezervă în `BudgetLedger` ÎNAINTE de a porni (atomic în
     asyncio) și e refuzat TYPED când plafonul e atins — modelul primește un text scurt și onest,
     nu un timeout. Peste deadline, o MUTAȚIE nu mai pornește deloc: mai bine „n-am apucat" decât o
     scriere a cărei confirmare nu mai ajunge la client.
  3. **Plafon de rezultat.** Un `llm_view` de 200KB nu e un rezultat, e un prompt otrăvit: se
     trunchiază bounded, cu metadate de acoperire, ÎNAINTE de a intra în conversație.

Poarta de paralelism e un reader-writer: citirile independente rulează concurent până la plafonul
clasei, mutațiile sunt EXCLUSIVE (nicio citire în zbor, nicio a doua mutație). Cu paralelismul
stins, plafonul e 1 → exact serializarea de azi (`ToolRun._execution_lock`), byte-identic.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from enum import Enum

from src.agent.tool_definitions import TOOL_NAMES
from src.runtime import turn_budget
from src.runtime.deadline import TurnDeadline

#: Text ONEST către model când un tool e refuzat de buget. Nu e o eroare de tool (n-a rulat) și nu
#: e o invitație la retry: modelul trebuie să încheie cu ce are (P6 — răspuns, nu tăcere).
REFUSAL_BUDGET = "(buget de tool epuizat pentru acest tur — răspunde cu informațiile deja obținute)"
REFUSAL_DEADLINE = "(nu mai e timp în acest tur — răspunde cu informațiile deja obținute)"


class ToolKind(str, Enum):
    READ = "read"
    MUTATION = "mutation"


class ToolPriority(str, Enum):
    CRITICAL = "critical"  # exact ce a cerut clientul — fără el răspunsul nu există
    OPTIONAL = "optional"  # îmbogățire; se poate sări când timpul e strâns


@dataclass(frozen=True, slots=True)
class ToolSpec:
    name: str
    kind: ToolKind
    parallel_safe: bool
    idempotent: bool
    priority: ToolPriority

    @property
    def is_mutation(self) -> bool:
        return self.kind is ToolKind.MUTATION


def _spec(name, kind, parallel_safe, idempotent, priority) -> ToolSpec:
    return ToolSpec(name, kind, parallel_safe, idempotent, priority)


#: Registrul de clasificare. `idempotent` la mutații NU e o speranță: `cart_add`/`checkout_link` au
#: receipts idempotente per (tur, acțiune) (NX-237), `subscribe_back_in_stock` are UNIQUE pe
#: (business, contact, produs, variantă).
_SPECS: dict[str, ToolSpec] = {
    s.name: s
    for s in (
        _spec("search_products", ToolKind.READ, True, True, ToolPriority.CRITICAL),
        _spec("get_product_details", ToolKind.READ, True, True, ToolPriority.CRITICAL),
        _spec("compare_products", ToolKind.READ, True, True, ToolPriority.OPTIONAL),
        _spec("faq_lookup", ToolKind.READ, True, True, ToolPriority.CRITICAL),
        _spec("check_order", ToolKind.READ, True, True, ToolPriority.CRITICAL),
        _spec("reorder", ToolKind.READ, True, True, ToolPriority.OPTIONAL),
        _spec("cart_add", ToolKind.MUTATION, False, True, ToolPriority.CRITICAL),
        _spec("checkout_link", ToolKind.MUTATION, False, True, ToolPriority.CRITICAL),
        _spec("subscribe_back_in_stock", ToolKind.MUTATION, False, True, ToolPriority.CRITICAL),
    )
}


def assert_registry_complete() -> None:
    """Poartă de import: fiecare tool pe care îl poate chema modelul ARE clasificare.

    Un tool nou fără linie aici ar rula ca „read paralel-safe" din inerție — adică exact felul în
    care o mutație ajunge să fie lansată speculativ. Mai bine crapă la import (ca
    `assert_registry_disjoint`, NX-236)."""
    missing = sorted(set(TOOL_NAMES) - set(_SPECS))
    if missing:
        raise RuntimeError(
            f"NX-241: tool-uri fără clasificare read/mutation: {missing}. Adaugă-le în "
            "src/agent/tool_budget._SPECS (kind, parallel_safe, idempotent, priority)."
        )
    unknown = sorted(set(_SPECS) - set(TOOL_NAMES))
    if unknown:
        raise RuntimeError(f"NX-241: clasificare pentru tool-uri inexistente: {unknown}")


assert_registry_complete()


def spec_for(name: str) -> ToolSpec:
    """Clasificarea unui tool. Un nume necunoscut (nu poate veni de la model — schemele sunt
    închise) e tratat ca MUTAȚIE necritică: conservator, nu permisiv."""
    known = _SPECS.get(name)
    if known is not None:
        return known
    return ToolSpec(name, ToolKind.MUTATION, False, False, ToolPriority.OPTIONAL)


@dataclass(frozen=True, slots=True)
class ToolAdmission:
    """Verdictul de admitere. `refusal` e textul dat modelului (None când e admis)."""

    allowed: bool
    reason: str | None = None
    refusal: str | None = None

    def __bool__(self) -> bool:
        return self.allowed


def admit(
    name: str,
    *,
    ledger: turn_budget.BudgetLedger | None = None,
    deadline: TurnDeadline | None = None,
) -> ToolAdmission:
    """Poate porni tool-ul ăsta ACUM? Fără ledger și fără deadline → mereu DA (flag stins).

    Ordinea contează: întâi TIMPUL (o mutație pornită după deadline produce un efect despre care
    clientul nu mai află), apoi BUGETUL (plafonul de apeluri).
    """
    tool = spec_for(name)
    if deadline is not None and not deadline.unbounded:
        cp = deadline.has_room_for("tools")
        if cp.exhausted:
            return ToolAdmission(False, f"deadline_{cp.reason}", REFUSAL_DEADLINE)
    if ledger is None:
        return ToolAdmission(True)
    decision = ledger.reserve("tool_calls")
    if not decision:
        return ToolAdmission(False, decision.reason or "tool_calls", REFUSAL_BUDGET)
    if tool.is_mutation:
        mutation = ledger.reserve("mutations")
        if not mutation:
            ledger.release("tool_calls")
            return ToolAdmission(False, mutation.reason or "mutations", REFUSAL_BUDGET)
    return ToolAdmission(True)


def cap_result(view: str, max_bytes: int) -> tuple[str, int]:
    """Trunchiere BOUNDED a rezultatului dat modelului, cu metadate de acoperire.

    Întoarce `(text, octeți_tăiați)`. Tăiem pe octeți (UTF-8), nu pe caractere: plafonul de prompt
    e în octeți/tokeni, iar diacriticele românești ar face un cap „pe caractere" să mintă. Nota de
    acoperire e explicită — modelul trebuie să ȘTIE că a văzut o parte, nu să creadă că a văzut tot.
    """
    if max_bytes <= 0:
        return view, 0
    raw = view.encode("utf-8")
    if len(raw) <= max_bytes:
        return view, 0
    dropped = len(raw) - max_bytes
    kept = raw[:max_bytes].decode("utf-8", errors="ignore")
    return f"{kept}\n(rezultat trunchiat: {dropped} octeți omiși din {len(raw)})", dropped


class ToolGate:
    """Poarta de execuție: citiri concurente până la plafon, mutații EXCLUSIVE și seriale.

    Reader-writer clasic, corect în asyncio (single-thread → verificările dintre `await`-uri sunt
    atomice). Cu `max_parallel_reads=1` se comportă exact ca lock-ul de azi.

    Invariantele:
      • o mutație nu pornește cât timp există o citire în zbor (fără efect pe stare pe jumătate
        citită) și nici o citire nu pornește cât timp o mutație e activă/în așteptare;
      • două mutații nu se suprapun niciodată (`_write_lock`), deci nu există „coș dublat";
      • un `finally` eliberează mereu — o excepție de tool nu blochează turul.
    """

    def __init__(self, max_parallel_reads: int = 1) -> None:
        self._sem = asyncio.Semaphore(max(1, max_parallel_reads))
        self._write_lock = asyncio.Lock()
        self._readers = 0
        self._writer = False
        self._idle = asyncio.Event()
        self._idle.set()
        self._writer_done = asyncio.Event()
        self._writer_done.set()
        self.peak_readers = 0

    async def _acquire_read(self) -> None:
        while self._writer:
            await self._writer_done.wait()
        await self._sem.acquire()
        self._readers += 1
        self.peak_readers = max(self.peak_readers, self._readers)
        self._idle.clear()

    def _release_read(self) -> None:
        self._readers = max(0, self._readers - 1)
        self._sem.release()
        if self._readers == 0:
            self._idle.set()

    async def _acquire_write(self) -> None:
        await self._write_lock.acquire()
        self._writer = True
        self._writer_done.clear()
        await self._idle.wait()  # lasă citirile deja pornite să se termine

    def _release_write(self) -> None:
        self._writer = False
        self._writer_done.set()
        self._write_lock.release()

    def hold(self, name: str) -> _ToolHold:
        """`async with gate.hold("cart_add"): ...` — alege singur regimul după clasificare."""
        return _ToolHold(self, spec_for(name))


class _ToolHold:
    __slots__ = ("_gate", "_spec", "_write")

    def __init__(self, gate: ToolGate, spec: ToolSpec) -> None:
        self._gate = gate
        self._spec = spec
        self._write = spec.is_mutation or not spec.parallel_safe

    async def __aenter__(self) -> ToolSpec:
        if self._write:
            await self._gate._acquire_write()
        else:
            await self._gate._acquire_read()
        return self._spec

    async def __aexit__(self, *exc) -> None:
        if self._write:
            self._gate._release_write()
        else:
            self._gate._release_read()
