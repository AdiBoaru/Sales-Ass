"""NX-241 — `TurnExecutionBudget`: plafoanele EXPLICITE ale unui tur, versionate pe clase.

Deadline-ul (`deadline.py`) spune cât timp mai am. Bugetul spune de câte ori am voie: runde de
model, tool calls, mutații, repair-uri, tokeni, cost, query-uri DB, octeți de rezultat. Fără el,
un model care intră în buclă („mai caută încă o dată") consumă bani și latență până la deadline,
iar deadline-ul singur nu poate distinge un tur legitim complicat de o furtună de tool calls.

Trei reguli care fac diferența față de un contor pus la întâmplare:

  1. **Manifestul e VERSIONAT.** `BUDGET_MANIFEST_VERSION` se schimbă odată cu tabelul; versiunea
     se capturează în evenimentul turului, deci un raport de latență spune pe ce plafoane a rulat.
     Fără asta, „p90 a crescut" nu se poate atribui niciodată unei schimbări de buget.
  2. **Modelul nu poate cere mai mult.** Plafoanele vin din config/cod, NICIODATĂ din outputul
     modelului (ca `business_id`, P7). Un plan care cere 12 tool calls primește 4 și un refuz typed.
  3. **Rezervarea e ATOMICĂ prin construcție.** `reserve()` incrementează contorul ÎNAINTE de orice
     `await`, iar asyncio e single-thread → două tool calls pornite în aceeași rundă nu pot trece
     amândouă de ultimul slot. Eliberarea (`release`) există pentru operațiile care nici n-au
     pornit; ce s-a executat rămâne consumat, chiar dacă a eșuat (a costat timp real).

Clasele de tur sunt cele din SLO-ul Stage 1 (docs/NX-241-TURN-DEADLINE.md). Clasa se poate RE-LEGA
o dată, când ruta devine cunoscută (triaj/creier): un tur pornește pe clasa implicită și, aflând
că e o comparație complexă, primește plafoanele ei — contoarele consumate deja NU se pierd.
"""

from __future__ import annotations

import contextvars
from collections.abc import Iterable
from dataclasses import dataclass, field
from enum import Enum
from functools import lru_cache
from typing import Any

#: Versiunea manifestului. SE SCHIMBĂ la orice modificare a tabelului de mai jos — e capturată în
#: `turn_latency`/`turn_budget_exhausted` și în raportul probei, ca o cifră să fie atribuibilă.
BUDGET_MANIFEST_VERSION = "nx241.2026-08-16"


class TurnClass(str, Enum):
    """Clasa de tur = profilul de cost/latență pe care îl acceptăm. Etichetă low-cardinality."""

    EXACT = "exact"  # fapt exact / fast path read-only (preț, stoc, status comandă, FAQ)
    RECOMMENDATION = "recommendation"  # recomandare normală (un search + plan)
    COMPLEX = "complex"  # comparație / mixt (mai multe tool-uri, evidence bogat)
    MUTATION = "mutation"  # coș / checkout / abonare — are scrieri, deci seriale


def turn_class_for(obligations: Iterable[Any]) -> TurnClass:
    """Clasa turului, derivată DETERMINIST din obligațiile deja extrase de cod (NX-251).

    Sursa e cea corectă din trei motive: obligațiile sunt extrase din mesajul BRUT de cod pur,
    există înainte de orice apel de model, și sunt exact vocabularul pe care planul trebuie să-l
    acopere. A întreba un model „cât de greu e turul ăsta?" ar readuce cascada pe care D1 o
    interzice — și ar plăti un apel ca să afle dacă merită să plătească un apel.

    Ordinea e de la cel mai scump la cel mai ieftin, iar necunoscutul urcă, nu coboară: o
    obligație pe care n-o recunoaștem primește tratamentul bun, nu pe cel ieftin. Greșeala în
    direcția asta costă bani; greșeala invers costă răspunsul clientului."""
    items = [str(getattr(o, "kind", o) or "").strip().lower() for o in obligations]
    items = [k for k in items if k]
    kinds = set(items)
    if not kinds:
        # Fără obligații extrase nu știm ce cere turul. Nu e „simplu" — e necunoscut.
        return TurnClass.RECOMMENDATION
    if "action" in kinds:
        return TurnClass.MUTATION
    if "compare" in kinds or len(items) > 1:
        # Mesaj mixt = mai multe lucruri de acoperit într-un singur răspuns: exact cazul în care
        # un model mai slab lasă pe dinafară jumătate din întrebare.
        # Numărăm OBLIGAȚIILE, nu tipurile lor: „ce preț are X și ai ceva pentru ten uscat?" are
        # două obligații `answer`, deci un set de tipuri de dimensiune 1 — pe tipuri, mesajul ăsta
        # ar fi coborât la „exact", adică fix cazul mixt ar fi primit modelul ieftin.
        return TurnClass.COMPLEX
    if kinds <= {"answer", "clarify", "explain", "safety"}:
        return TurnClass.EXACT
    return TurnClass.RECOMMENDATION


#: Dimensiunile bugetate — vocabular ÎNCHIS (`turn_budget_exhausted{dimension}`).
DIMENSIONS: tuple[str, ...] = (
    "model_rounds",
    "tool_calls",
    "parallel_reads",
    "mutations",
    "repair_calls",
    "critic_calls",
    "query_calls",
    "tokens",
    "cost_usd",
    "result_bytes",
)


@dataclass(frozen=True, slots=True)
class TurnExecutionBudget:
    """Plafoanele UNEI clase de tur. Milisecundele sunt CAPURI PE FAZĂ (nu o alocare care se
    adună la total): deadline-ul rămâne singura sumă, faza doar nu are voie să-l mănânce singură."""

    turn_class: TurnClass
    total_ms: int
    model_ms: int
    retrieval_ms: int
    tools_ms: int
    validation_ms: int
    terminal_reserve_ms: int
    max_model_rounds: int
    max_tool_calls: int
    max_parallel_reads: int
    max_mutations: int
    max_repair_calls: int
    max_critic_calls: int
    max_query_calls: int
    max_tokens: int
    max_cost_usd: float
    max_result_bytes: int
    max_evidence_items: int
    version: str = BUDGET_MANIFEST_VERSION

    def cap_for(self, dimension: str) -> float:
        """Plafonul unei dimensiuni. `KeyError` pentru un nume din afara vocabularului — o
        dimensiune inventată la runtime e un bug, nu o degradare tăcută."""
        caps: dict[str, float] = {
            "model_rounds": self.max_model_rounds,
            "tool_calls": self.max_tool_calls,
            "parallel_reads": self.max_parallel_reads,
            "mutations": self.max_mutations,
            "repair_calls": self.max_repair_calls,
            "critic_calls": self.max_critic_calls,
            "query_calls": self.max_query_calls,
            "tokens": self.max_tokens,
            "cost_usd": self.max_cost_usd,
            "result_bytes": self.max_result_bytes,
        }
        return caps[dimension]

    def validate(self) -> None:
        """Fail-fast (poartă de boot): un buget invalid e o config care ANULEAZĂ cardul, nu o
        nuanță. Mai bine crapă la pornire decât să ruleze „nelimitat" în tăcere."""
        if self.total_ms <= 0:
            raise ValueError(f"buget {self.turn_class.value}: total_ms trebuie > 0")
        if self.terminal_reserve_ms <= 0 or self.terminal_reserve_ms >= self.total_ms:
            raise ValueError(
                f"buget {self.turn_class.value}: rezerva terminală ({self.terminal_reserve_ms}ms) "
                f"trebuie să fie >0 și sub total ({self.total_ms}ms) — altfel commitul terminal "
                "nu are timp garantat"
            )
        for name in ("model_ms", "retrieval_ms", "tools_ms", "validation_ms"):
            value = getattr(self, name)
            if value <= 0 or value > self.total_ms:
                raise ValueError(
                    f"buget {self.turn_class.value}: {name}={value} trebuie în (0, {self.total_ms}]"
                )
        if self.max_model_rounds < 1 or self.max_tool_calls < 0 or self.max_parallel_reads < 1:
            raise ValueError(f"buget {self.turn_class.value}: plafoane de apeluri invalide")
        if self.max_repair_calls > 1:
            raise ValueError(
                f"buget {self.turn_class.value}: repair ≤ 1 (NX-239) — mai multe repair-uri sunt "
                "o buclă de orchestrare, nu o reparație"
            )
        if self.max_cost_usd <= 0 or self.max_tokens < 1 or self.max_result_bytes < 256:
            raise ValueError(f"buget {self.turn_class.value}: plafoane de cost/volum invalide")


#: Tabelul de bază, în COD (versionat cu `BUDGET_MANIFEST_VERSION`). Config-ul poate scala totalul
#: per clasă (env), dar forma și rapoartele stau aici: un plafon presărat prin cod e exact ce a
#: produs situația de dinainte de card.
_BASE: dict[TurnClass, dict[str, int | float]] = {
    TurnClass.EXACT: {
        "total_ms": 3_000,
        "model_ms": 2_000,
        "retrieval_ms": 800,
        "tools_ms": 1_200,
        "validation_ms": 300,
        "terminal_reserve_ms": 400,
        "max_model_rounds": 1,
        "max_tool_calls": 2,
        "max_parallel_reads": 2,
        "max_mutations": 0,
        "max_repair_calls": 0,
        "max_critic_calls": 0,
        "max_query_calls": 12,
        "max_tokens": 6_000,
        "max_result_bytes": 8_000,
        "max_evidence_items": 12,
    },
    TurnClass.RECOMMENDATION: {
        "total_ms": 6_000,
        "model_ms": 4_500,
        "retrieval_ms": 1_500,
        "tools_ms": 2_500,
        "validation_ms": 500,
        "terminal_reserve_ms": 600,
        "max_model_rounds": 2,
        "max_tool_calls": 4,
        "max_parallel_reads": 3,
        "max_mutations": 0,
        "max_repair_calls": 1,
        "max_critic_calls": 1,
        "max_query_calls": 20,
        "max_tokens": 12_000,
        "max_result_bytes": 16_000,
        "max_evidence_items": 24,
    },
    TurnClass.COMPLEX: {
        "total_ms": 10_000,
        "model_ms": 7_000,
        "retrieval_ms": 2_500,
        "tools_ms": 4_000,
        "validation_ms": 800,
        "terminal_reserve_ms": 800,
        "max_model_rounds": 3,
        "max_tool_calls": 6,
        "max_parallel_reads": 3,
        "max_mutations": 0,
        "max_repair_calls": 1,
        "max_critic_calls": 1,
        "max_query_calls": 30,
        "max_tokens": 20_000,
        "max_result_bytes": 24_000,
        "max_evidence_items": 36,
    },
    TurnClass.MUTATION: {
        "total_ms": 8_000,
        "model_ms": 5_000,
        "retrieval_ms": 1_500,
        "tools_ms": 3_000,
        "validation_ms": 600,
        "terminal_reserve_ms": 800,
        "max_model_rounds": 2,
        "max_tool_calls": 4,
        "max_parallel_reads": 2,
        "max_mutations": 2,
        "max_repair_calls": 1,
        "max_critic_calls": 1,
        "max_query_calls": 24,
        "max_tokens": 12_000,
        "max_result_bytes": 16_000,
        "max_evidence_items": 24,
    },
}


def build_manifest(
    *,
    totals_ms: dict[TurnClass, int] | None = None,
    hard_cap_ms: int,
    cost_ceiling_usd: float,
) -> dict[TurnClass, TurnExecutionBudget]:
    """Manifestul complet, validat. `totals_ms` scalează totalul unei clase (config), restul
    rapoartelor se recalculează proporțional → nu poți seta un total fără rezervă terminală.

    Ridică `ValueError` la config invalidă: poarta de boot o transformă în crash la pornire, care
    e EXACT ce vrem (alternativa e „nelimitat", adică fix problema de dinainte de card).
    """
    manifest: dict[TurnClass, TurnExecutionBudget] = {}
    for turn_class, base in _BASE.items():
        base_total = int(base["total_ms"])
        total = int((totals_ms or {}).get(turn_class, base_total))
        if total <= 0:
            raise ValueError(f"buget {turn_class.value}: total_ms trebuie > 0 (primit {total})")
        total = min(total, hard_cap_ms)
        scale = total / base_total
        budget = TurnExecutionBudget(
            turn_class=turn_class,
            total_ms=total,
            model_ms=max(1, round(int(base["model_ms"]) * scale)),
            retrieval_ms=max(1, round(int(base["retrieval_ms"]) * scale)),
            tools_ms=max(1, round(int(base["tools_ms"]) * scale)),
            validation_ms=max(1, round(int(base["validation_ms"]) * scale)),
            terminal_reserve_ms=max(1, round(int(base["terminal_reserve_ms"]) * scale)),
            max_model_rounds=int(base["max_model_rounds"]),
            max_tool_calls=int(base["max_tool_calls"]),
            max_parallel_reads=int(base["max_parallel_reads"]),
            max_mutations=int(base["max_mutations"]),
            max_repair_calls=int(base["max_repair_calls"]),
            max_critic_calls=int(base["max_critic_calls"]),
            max_query_calls=int(base["max_query_calls"]),
            max_tokens=int(base["max_tokens"]),
            max_cost_usd=cost_ceiling_usd,
            max_result_bytes=int(base["max_result_bytes"]),
            max_evidence_items=int(base["max_evidence_items"]),
        )
        budget.validate()
        manifest[turn_class] = budget
    return manifest


@lru_cache(maxsize=8)
def _manifest_cached(
    totals: tuple[tuple[str, int], ...], hard_cap_ms: int, cost_ceiling_usd: float
) -> dict[TurnClass, TurnExecutionBudget]:
    return build_manifest(
        totals_ms={TurnClass(name): ms for name, ms in totals},
        hard_cap_ms=hard_cap_ms,
        cost_ceiling_usd=cost_ceiling_usd,
    )


def manifest_from_settings(settings: object) -> dict[TurnClass, TurnExecutionBudget]:
    """Manifestul configurat (cache-uit pe valorile scalare — se construiește o dată per proces).

    Ridică `ValueError` la config invalidă; poarta de boot din `Settings` îl cheamă exact ca să se
    întâmple asta la pornire, nu la primul tur."""
    totals = (
        (TurnClass.EXACT.value, int(getattr(settings, "turn_budget_exact_ms", 3_000))),
        (
            TurnClass.RECOMMENDATION.value,
            int(getattr(settings, "turn_budget_recommendation_ms", 6_000)),
        ),
        (TurnClass.COMPLEX.value, int(getattr(settings, "turn_budget_complex_ms", 10_000))),
        (TurnClass.MUTATION.value, int(getattr(settings, "turn_budget_mutation_ms", 8_000))),
    )
    return _manifest_cached(
        totals,
        int(getattr(settings, "turn_hard_deadline_ms", 15_000)),
        float(getattr(settings, "turn_cost_budget_usd", 0.01)),
    )


def budget_for(turn_class: TurnClass, settings: object) -> TurnExecutionBudget:
    return manifest_from_settings(settings)[turn_class]


def classify(
    route: str | None,
    *,
    has_action: bool = False,
    purchase_intent: bool = False,
    compare: bool = False,
) -> TurnClass:
    """Clasa de tur din SEMNALE DETERMINISTE (rută, acțiune opacă, intenție de cumpărare).

    Pur (primitive in, enum out) ca să nu lege bugetul de `TurnContext`: îl folosesc și runner-ul,
    și proba de latență, și testele. Ordinea e de la cel mai angajant la cel mai ieftin — o mutație
    rămâne mutație chiar dacă mesajul arată ca o comparație.
    """
    if has_action or purchase_intent:
        return TurnClass.MUTATION
    if compare:
        return TurnClass.COMPLEX
    if route in ("simple", "order", "clarify", "handoff"):
        return TurnClass.EXACT
    return TurnClass.RECOMMENDATION


@dataclass(frozen=True, slots=True)
class BudgetDecision:
    """Verdictul unei rezervări. `reason` e low-cardinality (dimensiunea epuizată)."""

    allowed: bool
    dimension: str
    reason: str | None = None
    remaining: float = 0.0

    def __bool__(self) -> bool:
        return self.allowed


@dataclass
class BudgetLedger:
    """Contoarele UNUI tur față de bugetul clasei lui.

    `enforced=False` = observă și numără, dar spune mereu DA (modul observe-only din rollout).
    Contoarele sunt aceleași în ambele moduri — de asta un raport „observe" poate prezice ce ar fi
    respins „enforce", fără să schimbe nimic pentru client.
    """

    budget: TurnExecutionBudget
    enforced: bool = False
    spent: dict[str, float] = field(default_factory=dict)
    rejections: dict[str, int] = field(default_factory=dict)
    #: Vârfuri utile în raport (nu resetate de `release`).
    peak_parallel_reads: int = 0

    # ── rezervare / consum ─────────────────────────────────────────────────────────────────
    def reserve(self, dimension: str, amount: float = 1.0) -> BudgetDecision:
        """Rezervă ÎNAINTE de a porni operația. Incrementul se face fără niciun `await` între
        verificare și scriere → în asyncio e atomic, deci N apeluri lansate simultan nu pot trece
        toate de ultimul slot (exact scenariul „tool storm cerut de model")."""
        cap = self.budget.cap_for(dimension)
        used = self.spent.get(dimension, 0.0)
        if used + amount > cap:
            self.rejections[dimension] = self.rejections.get(dimension, 0) + 1
            if self.enforced:
                return BudgetDecision(False, dimension, dimension, max(0.0, cap - used))
        self.spent[dimension] = used + amount
        if dimension == "parallel_reads":
            self.peak_parallel_reads = max(self.peak_parallel_reads, int(self.spent[dimension]))
        return BudgetDecision(True, dimension, None, max(0.0, cap - used - amount))

    def release(self, dimension: str, amount: float = 1.0) -> None:
        """Eliberează o rezervare care NU s-a executat (ex. slotul de paralelism, după ce tool-ul
        s-a terminat). Ce s-a executat rămâne consumat, chiar dacă a eșuat — a costat timp real."""
        self.spent[dimension] = max(0.0, self.spent.get(dimension, 0.0) - amount)

    def consume(self, dimension: str, amount: float) -> None:
        """Raportare POST-factum (tokeni/cost/query-uri măsurați după apel): numără, nu refuză."""
        self.spent[dimension] = self.spent.get(dimension, 0.0) + amount

    # ── interogare ─────────────────────────────────────────────────────────────────────────
    def remaining(self, dimension: str) -> float:
        return max(0.0, self.budget.cap_for(dimension) - self.spent.get(dimension, 0.0))

    def exhausted(self, dimension: str) -> bool:
        return self.remaining(dimension) <= 0

    def rebind(self, budget: TurnExecutionBudget) -> None:
        """Re-leagă clasa (ruta devine cunoscută abia după triaj/creier). Contoarele consumate
        RĂMÂN: un tur nu-și șterge istoria pentru că s-a reclasificat."""
        self.budget = budget

    def as_event_props(self) -> dict[str, object]:
        """Props pentru `turn_latency` / raport: doar numere + nume din vocabular (P12)."""
        props: dict[str, object] = {
            "budget_version": self.budget.version,
            "turn_class": self.budget.turn_class.value,
            "budget_enforced": self.enforced,
            "model_rounds": int(self.spent.get("model_rounds", 0)),
            "tool_calls": int(self.spent.get("tool_calls", 0)),
            "mutations": int(self.spent.get("mutations", 0)),
            "query_calls": int(self.spent.get("query_calls", 0)),
            "tokens": int(self.spent.get("tokens", 0)),
            "cost_usd": round(self.spent.get("cost_usd", 0.0), 6),
            "peak_parallel_reads": self.peak_parallel_reads,
        }
        if self.rejections:
            props["budget_rejections"] = dict(sorted(self.rejections.items()))
        return props


# ── ContextVar (ca deadline-ul): turul îl împinge, operațiile îl citesc ────────────────────
_current: contextvars.ContextVar[BudgetLedger | None] = contextvars.ContextVar(
    "turn_budget_ledger", default=None
)


def push(ledger: BudgetLedger | None) -> contextvars.Token:
    return _current.set(ledger)


def pop(token: contextvars.Token) -> None:
    _current.reset(token)


def current() -> BudgetLedger | None:
    """Ledgerul turului curent, sau `None` (fără tur / flag stins) → nimeni nu impune nimic."""
    return _current.get()


def reserve(dimension: str, amount: float = 1.0) -> BudgetDecision:
    """Scurtătură pentru apelanți: rezervă pe ledgerul curent; fără ledger întoarce DA."""
    ledger = current()
    if ledger is None:
        return BudgetDecision(True, dimension)
    return ledger.reserve(dimension, amount)


def consume(dimension: str, amount: float) -> None:
    ledger = current()
    if ledger is not None:
        ledger.consume(dimension, amount)


def count_bucket(n: int) -> str:
    """Bandă low-cardinality pentru contoare (runde/tool calls) în metrici."""
    if n <= 0:
        return "0"
    if n == 1:
        return "1"
    if n == 2:
        return "2"
    if n <= 4:
        return "3-4"
    if n <= 8:
        return "5-8"
    return "9+"
