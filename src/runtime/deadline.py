"""NX-241 — `TurnDeadline`: UN singur buget de timp pentru tot turul, în ceas MONOTON.

Problema pe care o rezolvă: azi fiecare strat are timeoutul LUI (`llm_timeout_s=30` × `retry_max=2`
per apel, `embed_timeout_ms`, `retrieval_deadline_ms`, `web_turn_deadline_s=120`), iar un tur face
mai multe apeluri. Timeouturile se ÎNMULȚESC, nu se împart: un singur provider lent poate consuma
90s pe un tur pe care clientul îl așteaptă de 3s. Nimeni nu întreabă „cât mai am".

Contractul de aici:

  • Deadline-ul se naște O DATĂ, din `web_turns.deadline_at` (fixat la accept, NX-233), și NU se
    prelungește la reclaim/retry — altfel un turn crăpat de 5 ori ar avea 5×15s pe seama aceluiași
    client care așteaptă.
  • Așteptarea în coadă (admission, claim) CONSUMĂ același buget: `from_deadline_at` primește
    `elapsed_ms`, deci timpul scurs până la claim e deja scăzut.
  • Ceas MONOTON, nu wall-clock: un pas NTP în mijlocul turului ar face un deadline să expire
    instant sau niciodată. Wall-clock-ul se folosește O SINGURĂ dată — ca să traducem `deadline_at`
    (care e un timestamp durabil, comparabil între procese) într-un buget de milisecunde.
  • REZERVA terminală: `remaining_ms()` întoarce implicit timpul rămas MINUS rezerva pentru
    validator + fallback + commitul terminal. Un tur nu are voie să cheltuie ultimul milisecund pe
    model și să rămână fără timp să SCRIE ceva onest (P6: niciodată tăcere).
  • Clock injectabil → testele rulează cu ceas fals, nu cu `sleep`.

Fără tur activ (`current()` e `None`) sau cu `total_ms <= 0`, totul e no-op: `timeout_for` întoarce
`None`, iar apelanții se comportă exact ca înainte (kill-switch numeric, ca `embed_timeout_ms`).
"""

from __future__ import annotations

import asyncio
import contextvars
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from time import monotonic

#: Fazele unui tur — vocabular ÎNCHIS. Sunt etichete de metrică (`turn_deadline_exhausted{phase}`),
#: deci nu au voie să fie construite dinamic (P12: zero cardinalitate din date de client).
PHASES: tuple[str, ...] = (
    "queue",  # așteptarea admission/claim, DINAINTE de orice lucru
    "load",  # snapshot/istoric/config (NX-231, checkout-uri scurte)
    "gates",  # gates + limbă + straturi gratuite
    "retrieval",  # portul NX-238 (lexical + semantic + rerank)
    "model",  # apelurile de model (triaj, brain, repair)
    "tools",  # execuția tool-urilor deterministe
    "validation",  # validator + grounding guard (NX-240)
    "projection",  # projectorul PUR `web-view.v2`
    "commit",  # tranzacția terminală
    "aftercare",  # STRICT după terminal — nu ține niciodată clientul
)

#: Motivele pentru care un deadline oprește lucrul. Tot vocabular închis (etichetă de metrică).
REASON_EXPIRED = "expired"  # timpul s-a terminat efectiv
REASON_NO_ROOM = "no_room"  # ce urma nu ÎNCĂPEA în ce mai rămăsese (nu-l mai pornim degeaba)
REASON_CANCELLED = "cancelled"  # lease pierdut / shutdown — altcineva e autoritatea

#: Cât timp minim considerăm „util" pentru a mai PORNI o operație externă. Sub el, a porni un apel
#: pe care oricum îl vom anula costă bani și latență fără nicio șansă de rezultat.
MIN_USEFUL_MS = 150


class DeadlineExhausted(RuntimeError):
    """Bugetul de timp s-a terminat. `phase`/`reason` sunt low-cardinality (etichete de metrică).

    Se ridică DOAR de apelanții care aleg `raise_if_exhausted`; restul citesc `remaining_ms()` și
    degradează onest (P6). Niciodată o excepție care să iasă spre client fără ViewModel terminal.
    """

    def __init__(self, phase: str, reason: str = REASON_EXPIRED) -> None:
        super().__init__(f"deadline epuizat în faza {phase} ({reason})")
        self.phase = phase
        self.reason = reason


@dataclass(frozen=True, slots=True)
class Checkpoint:
    """Fotografia deadline-ului la intrarea într-o fază — pură, fără efect."""

    phase: str
    remaining_ms: int
    exhausted: bool
    reason: str | None = None


@dataclass(slots=True)
class TurnDeadline:
    """Bugetul de timp al UNUI tur. Mutabil doar prin `cancel()` (semnalul de anulare).

    `total_ms < 0` = DEZACTIVAT (folosește `TurnDeadline.disabled()`); `total_ms == 0` e altceva:
    un buget REAL care s-a terminat deja (un `deadline_at` trecut). Distincția nu e cosmetică — a
    le confunda ar transforma un tur expirat într-unul nelimitat, adică exact invers.

    `terminal_reserve_ms` e felia păstrată pentru validator + fallback + commit: `remaining_ms()` o
    scade implicit, `remaining_ms(reserve=False)` o arată (folosit EXACT de faza terminală, care
    are voie să o cheltuie).
    """

    total_ms: int
    terminal_reserve_ms: int = 0
    clock: Callable[[], float] = monotonic
    #: Milisecunde deja consumate ÎNAINTE de construcție (coadă, claim, reclaim precedent).
    elapsed_before_ms: int = 0
    started_at: float = field(default=0.0)
    _cancel: asyncio.Event | None = field(default=None, repr=False, compare=False)
    _cancel_reason: str | None = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        if not self.started_at:
            self.started_at = self.clock()
        if self.terminal_reserve_ms < 0:
            raise ValueError("terminal_reserve_ms nu poate fi negativ")

    # ── construcție ────────────────────────────────────────────────────────────────────────
    @classmethod
    def disabled(cls) -> TurnDeadline:
        """Deadline care nu impune nimic (test/compat). Nu-l împinge în ContextVar pe calea reală:
        prezența unui deadline ACTIVEAZĂ căile noi (admission de tool, poartă read/mutation) — un
        deadline nelimitat le-ar porni fără să limiteze nimic. Acolo folosește `None`."""
        return cls(total_ms=-1)

    @classmethod
    def from_deadline_at(
        cls,
        deadline_at: datetime | None,
        now: datetime,
        *,
        fallback_total_ms: int,
        hard_cap_ms: int,
        terminal_reserve_ms: int = 0,
        elapsed_ms: int = 0,
        clock: Callable[[], float] = monotonic,
    ) -> TurnDeadline:
        """Traduce `web_turns.deadline_at` (durabil, wall-clock) în buget monoton, O SINGURĂ dată.

        `deadline_at` lipsă (ledger OFF) → `fallback_total_ms`. Bugetul e mereu plafonat de
        `hard_cap_ms`: un `deadline_at` scris greșit (ceas sărit, config veche) nu are voie să
        producă un tur de o oră. Un deadline deja trecut dă `total_ms=0` — apelantul terminalizează
        onest, nu pornește lucru inutil.
        """
        if deadline_at is None:
            total = fallback_total_ms
        else:
            total = int((deadline_at - now).total_seconds() * 1000.0)
        total = max(0, min(total, hard_cap_ms))
        return cls(
            total_ms=total,
            terminal_reserve_ms=min(terminal_reserve_ms, max(0, total - 1)),
            clock=clock,
            elapsed_before_ms=max(0, elapsed_ms),
        )

    # ── interogare ─────────────────────────────────────────────────────────────────────────
    @property
    def unbounded(self) -> bool:
        """Fără buget = nu impunem nimic. STRICT negativ: `0` înseamnă „buget consumat", nu
        „nelimitat" (vezi docstring-ul clasei)."""
        return self.total_ms < 0

    def elapsed_ms(self) -> int:
        """Timp consumat, INCLUSIV coada de dinainte de construcție."""
        return self.elapsed_before_ms + int((self.clock() - self.started_at) * 1000.0)

    def remaining_ms(self, *, reserve: bool = True, extra_reserve_ms: int = 0) -> int:
        """Milisecunde rămase. Implicit SCADE rezerva terminală. Niciodată negativ.

        Pe un deadline fără buget întoarce un număr mare (nu 0): apelanții compară cu plafoanele
        lor, iar „fără buget" înseamnă „nu te opresc eu", nu „nu mai ai timp".
        """
        if self.unbounded:
            return 2**31 - 1
        held = (self.terminal_reserve_ms if reserve else 0) + max(0, extra_reserve_ms)
        return max(0, self.total_ms - self.elapsed_ms() - held)

    def expired(self, *, reserve: bool = True) -> bool:
        return not self.unbounded and self.remaining_ms(reserve=reserve) <= 0

    def fits(self, cost_ms: float, *, reserve: bool = True, minimum_ms: int = 0) -> bool:
        """Încape o operație care costă `cost_ms` (ex. un `Retry-After`) + minimul ei util?

        Ăsta e testul care oprește „dormim 20s pentru un retry pe un tur cu 3s buget".
        """
        if self.unbounded:
            return True
        return self.remaining_ms(reserve=reserve) >= cost_ms + max(0, minimum_ms)

    def timeout_for(
        self,
        cap_ms: int | None = None,
        *,
        reserve: bool = True,
        extra_reserve_ms: int = 0,
    ) -> float | None:
        """Timeoutul (SECUNDE, pentru `asyncio`) al UNEI operații: `min(cap, remaining - reserve)`.

        `None` = fără buget ȘI fără cap → apelantul folosește ce folosea înainte. `0.0` = nu mai e
        timp: apelantul NU pornește operația, degradează (P6).
        """
        remaining = self.remaining_ms(reserve=reserve, extra_reserve_ms=extra_reserve_ms)
        if self.unbounded:
            return None if cap_ms is None else max(0.0, cap_ms / 1000.0)
        budget = remaining if cap_ms is None else min(remaining, cap_ms)
        return max(0.0, budget / 1000.0)

    def has_room_for(self, phase: str, *, minimum_ms: int = MIN_USEFUL_MS) -> Checkpoint:
        """Mai are rost să PORNIM faza asta? (`minimum_ms` = pragul de la care o operație externă
        are vreo șansă să se termine). Pur — nu schimbă nimic, doar spune."""
        if self.cancelled:
            reason = self._cancel_reason or REASON_CANCELLED
            return Checkpoint(phase, self.remaining_ms(), True, reason)
        remaining = self.remaining_ms()
        if self.unbounded:
            return Checkpoint(phase, remaining, False)
        if remaining <= 0:
            return Checkpoint(phase, remaining, True, REASON_EXPIRED)
        if remaining < minimum_ms:
            return Checkpoint(phase, remaining, True, REASON_NO_ROOM)
        return Checkpoint(phase, remaining, False)

    def checkpoint(self, phase: str) -> Checkpoint:
        """Fotografie la intrarea într-o fază (fără prag de utilitate — doar „mai am timp?")."""
        return self.has_room_for(phase, minimum_ms=0)

    def raise_if_exhausted(self, phase: str, *, minimum_ms: int = 0) -> None:
        cp = self.has_room_for(phase, minimum_ms=minimum_ms)
        if cp.exhausted:
            raise DeadlineExhausted(phase, cp.reason or REASON_EXPIRED)

    # ── anulare (lease pierdut / shutdown) ────────────────────────────────────────────────
    @property
    def cancel_event(self) -> asyncio.Event:
        """Semnalul de anulare, creat leneș (un `TurnDeadline` poate exista fără loop, în teste)."""
        if self._cancel is None:
            self._cancel = asyncio.Event()
        return self._cancel

    @property
    def cancelled(self) -> bool:
        return self._cancel is not None and self._cancel.is_set()

    def cancel(self, reason: str = REASON_CANCELLED) -> None:
        """Oprește tot ce mai citește deadline-ul. Idempotentă; NU anulează task-uri — asta rămâne
        treaba executorului (el deține task-ul pipeline-ului)."""
        self._cancel_reason = reason
        self.cancel_event.set()

    # ── raport ────────────────────────────────────────────────────────────────────────────
    def as_event_props(self) -> dict[str, int | bool]:
        """Props pentru evenimente/loguri: doar milisecunde și booleeni (P12)."""
        return {
            "deadline_total_ms": self.total_ms,
            "deadline_elapsed_ms": self.elapsed_ms(),
            "deadline_remaining_ms": self.remaining_ms(),
            "deadline_reserve_ms": self.terminal_reserve_ms,
            "deadline_cancelled": self.cancelled,
        }


# ── ContextVar: turul îl împinge o dată, operațiile îl citesc ──────────────────────────────
_current: contextvars.ContextVar[TurnDeadline | None] = contextvars.ContextVar(
    "turn_deadline", default=None
)


def push(deadline: TurnDeadline | None) -> contextvars.Token:
    """Leagă deadline-ul de turul curent. Simetric cu `usage.push()`."""
    return _current.set(deadline)


def pop(token: contextvars.Token) -> None:
    _current.reset(token)


def current() -> TurnDeadline | None:
    """Deadline-ul turului curent, sau `None` (job, script, boot, flag stins)."""
    return _current.get()


def timeout_for(cap_ms: int | None = None, *, reserve: bool = True) -> float | None:
    """Scurtătura folosită de adaptoare (LLM/embed/retrieval): timeoutul operației curente.

    `None` = niciun deadline activ → apelantul păstrează comportamentul lui de dinainte.
    """
    d = current()
    if d is None:
        return None
    return d.timeout_for(cap_ms, reserve=reserve)


def remaining_ms(*, reserve: bool = True) -> int | None:
    d = current()
    return None if d is None else d.remaining_ms(reserve=reserve)
