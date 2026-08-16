"""NX-241 — `TurnDeadline`: buget monoton, rezervă terminală, retry care NU-l depășește.

Totul pe CEAS FALS: un test care ar dormi ca să verifice un deadline măsoară mașina de CI, nu
codul. Adaptorul OpenAI e testat cu client fake (zero apeluri reale), ca `tests/test_llm.py`.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import httpx
import openai
import pytest

from src.agent import llm
from src.agent.llm import LLMClient
from src.runtime import deadline
from src.runtime.deadline import (
    MIN_USEFUL_MS,
    REASON_EXPIRED,
    REASON_NO_ROOM,
    DeadlineExhausted,
    TurnDeadline,
)


class FakeClock:
    """Ceas monoton controlat de test. `advance(ms)` = trece timpul, fără să dormim."""

    def __init__(self) -> None:
        self.t = 1_000.0

    def __call__(self) -> float:
        return self.t

    def advance(self, ms: float) -> None:
        self.t += ms / 1000.0


def _deadline(total_ms=6_000, reserve_ms=600) -> tuple[TurnDeadline, FakeClock]:
    clock = FakeClock()
    return TurnDeadline(total_ms=total_ms, terminal_reserve_ms=reserve_ms, clock=clock), clock


# ── bugetul de bază ────────────────────────────────────────────────────────────────────────
def test_remaining_subtracts_reserve_by_default():
    d, clock = _deadline()
    assert d.remaining_ms() == 5_400  # 6000 - 600 rezervă
    assert d.remaining_ms(reserve=False) == 6_000
    clock.advance(1_000)
    assert d.remaining_ms() == 4_400
    assert d.remaining_ms(reserve=False) == 5_000


def test_reserve_keeps_time_for_terminal_commit():
    """Momentul care contează: bugetul „de lucru" e epuizat, dar rezerva încă există — adică
    validatorul + fallbackul + commitul au timp GARANTAT să scrie ceva onest."""
    d, clock = _deadline(total_ms=3_000, reserve_ms=400)
    clock.advance(2_600)
    assert d.remaining_ms() == 0
    assert d.expired() is True
    assert d.remaining_ms(reserve=False) == 400
    assert d.expired(reserve=False) is False


def test_unbounded_deadline_never_stops_anything():
    d = TurnDeadline.disabled()
    assert d.unbounded
    assert not d.expired()
    assert d.timeout_for(None) is None
    assert d.timeout_for(2_000) == pytest.approx(2.0)
    assert d.has_room_for("model").exhausted is False


def test_timeout_for_is_the_minimum_of_cap_and_remaining():
    d, clock = _deadline(total_ms=6_000, reserve_ms=600)
    assert d.timeout_for(8_000) == pytest.approx(5.4)  # capul e mai mare → limitează bugetul
    assert d.timeout_for(1_000) == pytest.approx(1.0)  # capul e mai mic → limitează capul
    clock.advance(5_500)
    assert d.timeout_for(8_000) == 0.0  # nu mai e timp: apelantul NU pornește operația


def test_has_room_for_refuses_when_below_minimum_useful():
    d, clock = _deadline(total_ms=1_000, reserve_ms=100)
    clock.advance(800)
    cp = d.has_room_for("model", minimum_ms=MIN_USEFUL_MS)
    assert cp.exhausted and cp.reason == REASON_NO_ROOM  # mai sunt 100ms: nu are rost să pornim
    assert d.has_room_for("model", minimum_ms=0).exhausted is False


def test_expired_reports_expired_not_no_room():
    d, clock = _deadline(total_ms=1_000, reserve_ms=100)
    clock.advance(1_000)
    assert d.has_room_for("tools").reason == REASON_EXPIRED


def test_fits_is_what_stops_a_long_retry_after():
    d, _ = _deadline(total_ms=3_000, reserve_ms=400)
    assert d.fits(1_000, minimum_ms=600) is True
    assert d.fits(20_000, minimum_ms=600) is False  # Retry-After de 20s pe un tur de 3s


# ── construcție din ledger (`deadline_at`) ────────────────────────────────────────────────
def test_from_deadline_at_uses_the_absolute_ledger_value():
    now = datetime(2026, 8, 16, 12, 0, 0, tzinfo=UTC)
    d = TurnDeadline.from_deadline_at(
        now + timedelta(seconds=9),
        now,
        fallback_total_ms=120_000,
        hard_cap_ms=15_000,
        terminal_reserve_ms=600,
        clock=FakeClock(),
    )
    assert d.total_ms == 9_000
    assert d.remaining_ms() == 8_400


def test_from_deadline_at_is_capped_by_the_hard_deadline():
    """Un `deadline_at` absurd (ceas sărit/config veche) nu are voie să dea un tur de o oră."""
    now = datetime(2026, 8, 16, 12, 0, 0, tzinfo=UTC)
    d = TurnDeadline.from_deadline_at(
        now + timedelta(hours=1),
        now,
        fallback_total_ms=120_000,
        hard_cap_ms=15_000,
        terminal_reserve_ms=600,
        clock=FakeClock(),
    )
    assert d.total_ms == 15_000


def test_from_deadline_at_already_passed_gives_zero_budget():
    now = datetime(2026, 8, 16, 12, 0, 0, tzinfo=UTC)
    d = TurnDeadline.from_deadline_at(
        now - timedelta(seconds=5),
        now,
        fallback_total_ms=120_000,
        hard_cap_ms=15_000,
        terminal_reserve_ms=600,
        clock=FakeClock(),
    )
    assert d.total_ms == 0 and d.expired(reserve=False)


def test_queue_wait_consumes_the_same_budget_on_the_fallback_path():
    """Fără `deadline_at` (ledger OFF) așteptarea în coadă se scade EXPLICIT: altfel un turn care
    a stat 4s în coadă ar primi bugetul întreg, iar clientul ar aștepta suma."""
    now = datetime(2026, 8, 16, 12, 0, 0, tzinfo=UTC)
    d = TurnDeadline.from_deadline_at(
        None,
        now,
        fallback_total_ms=10_000,
        hard_cap_ms=15_000,
        terminal_reserve_ms=600,
        elapsed_ms=4_000,
        clock=FakeClock(),
    )
    assert d.total_ms == 10_000
    assert d.remaining_ms() == 5_400  # 10000 - 4000 coadă - 600 rezervă


def test_reclaim_does_not_grant_a_second_budget():
    """Un reclaim recalculează din ACELAȘI `deadline_at` absolut → al doilea worker primește ce a
    mai rămas, nu încă un buget întreg. Ăsta e invariantul pe care se sprijină toată socoteala."""
    accepted = datetime(2026, 8, 16, 12, 0, 0, tzinfo=UTC)
    deadline_at = accepted + timedelta(seconds=15)
    first = TurnDeadline.from_deadline_at(
        deadline_at, accepted, fallback_total_ms=1, hard_cap_ms=15_000, clock=FakeClock()
    )
    reclaimed = TurnDeadline.from_deadline_at(
        deadline_at,
        accepted + timedelta(seconds=11),
        fallback_total_ms=1,
        hard_cap_ms=15_000,
        clock=FakeClock(),
    )
    assert first.total_ms == 15_000
    assert reclaimed.total_ms == 4_000


# ── anulare ────────────────────────────────────────────────────────────────────────────────
def test_cancel_marks_every_phase_exhausted():
    d, _ = _deadline()
    assert d.cancelled is False
    d.cancel("cancelled")
    assert d.cancelled is True
    cp = d.has_room_for("tools")
    assert cp.exhausted and cp.reason == "cancelled"
    with pytest.raises(DeadlineExhausted):
        d.raise_if_exhausted("tools")


def test_contextvar_push_pop_is_scoped():
    assert deadline.current() is None
    d, _ = _deadline()
    token = deadline.push(d)
    try:
        assert deadline.current() is d
        assert deadline.timeout_for(1_000) == pytest.approx(1.0)
    finally:
        deadline.pop(token)
    assert deadline.current() is None
    assert deadline.timeout_for(1_000) is None  # fără tur → apelantul rămâne cu capul lui


# ── matricea de retry a adaptorului (client FAKE) ─────────────────────────────────────────
def _req():
    return httpx.Request("POST", "https://api.openai.com/v1/chat/completions")


def _rate_limit(retry_after="0"):
    return openai.RateLimitError(
        "rate limited",
        response=httpx.Response(429, headers={"retry-after": retry_after}, request=_req()),
        body=None,
    )


def _server_error():
    return openai.InternalServerError(
        "boom", response=httpx.Response(503, request=_req()), body=None
    )


def _auth_error():
    return openai.AuthenticationError(
        "nope", response=httpx.Response(401, request=_req()), body=None
    )


class _Msg:
    def __init__(self, content):
        self.content = content
        self.tool_calls = None


class _Resp:
    def __init__(self, content="ok"):
        self.choices = [SimpleNamespace(message=_Msg(content))]


class _Completions:
    def __init__(self, behaviors):
        self._behaviors = list(behaviors)
        self.calls = 0

    async def create(self, **kwargs):  # noqa: ARG002
        self.calls += 1
        b = self._behaviors.pop(0)
        if isinstance(b, Exception):
            raise b
        return b


def _client(behaviors):
    comp = _Completions(behaviors)
    return LLMClient(
        SimpleNamespace(chat=SimpleNamespace(completions=comp)),
        model_triage="nano",
        model_agent="mini",
    ), comp


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    """Somnul de backoff devine no-op: măsurăm DECIZIA de a dormi, nu durata ei."""
    slept: list[float] = []

    async def _sleep(s):
        slept.append(s)

    monkeypatch.setattr(llm.asyncio, "sleep", _sleep)
    monkeypatch.setattr(llm.usage, "record_chat", lambda *a, **k: None)
    return slept


@pytest.mark.asyncio
async def test_retry_matrix_without_deadline_is_unchanged(_no_sleep):
    """Fără deadline activ: 429 → retry până la plafon, exact ca NX-126."""
    client, comp = _client([_rate_limit(), _server_error(), _Resp()])
    assert await client.complete("s", "u") == "ok"
    assert comp.calls == 3
    assert len(_no_sleep) == 2


@pytest.mark.asyncio
async def test_auth_error_is_never_retried(_no_sleep):
    client, comp = _client([_auth_error()])
    with pytest.raises(openai.AuthenticationError):
        await client.complete("s", "u")
    assert comp.calls == 1 and _no_sleep == []


@pytest.mark.asyncio
async def test_retry_after_longer_than_remaining_does_not_sleep(_no_sleep):
    """429 cu `Retry-After: 30` pe un tur care mai are 2s: NU dormim, degradăm terminal.
    Ăsta e cazul din matricea de eșecuri — un retry „corect" care ar rata clientul cu 28s."""
    d, _ = _deadline(total_ms=2_000, reserve_ms=400)
    token = deadline.push(d)
    try:
        client, comp = _client([_rate_limit(retry_after="30"), _Resp()])
        with pytest.raises(openai.RateLimitError):
            await client.complete("s", "u")
        assert comp.calls == 1  # a doua încercare nici nu s-a făcut
        assert _no_sleep == []  # și nici nu am dormit degeaba
    finally:
        deadline.pop(token)


@pytest.mark.asyncio
async def test_retry_after_that_fits_is_respected(_no_sleep):
    d, _ = _deadline(total_ms=10_000, reserve_ms=600)
    token = deadline.push(d)
    try:
        client, comp = _client([_rate_limit(retry_after="1"), _Resp()])
        assert await client.complete("s", "u") == "ok"
        assert comp.calls == 2
        assert _no_sleep and 1.0 <= _no_sleep[0] <= 1.3  # Retry-After + jitter bounded
    finally:
        deadline.pop(token)


@pytest.mark.asyncio
async def test_no_call_is_started_without_remaining_budget(_no_sleep):
    """Deadline consumat înainte de apel: nu pornim nimic. Un apel pe care oricum îl anulăm costă
    bani și latență fără nicio șansă de rezultat."""
    d, clock = _deadline(total_ms=1_000, reserve_ms=200)
    clock.advance(1_000)
    token = deadline.push(d)
    try:
        client, comp = _client([_Resp()])
        with pytest.raises(DeadlineExhausted) as exc:
            await client.complete("s", "u")
        assert exc.value.reason == REASON_NO_ROOM
        assert comp.calls == 0
    finally:
        deadline.pop(token)


@pytest.mark.asyncio
async def test_slow_call_is_cut_by_the_turn_deadline(monkeypatch, _no_sleep):
    """Un provider care atârnă e tăiat de `asyncio.timeout` derivat din bugetul turului — nu de
    `llm_timeout_s` × retry (care ar fi însemnat ~90s pe un tur de 1s)."""
    import asyncio

    class _Hanging:
        calls = 0

        async def create(self, **kwargs):  # noqa: ARG002
            # `Event().wait()` (nu `sleep`): fixture-ul de mai sus a înlocuit `asyncio.sleep` cu un
            # no-op, iar noi vrem un apel care chiar ATÂRNĂ până îl taie deadline-ul.
            _Hanging.calls += 1
            await asyncio.Event().wait()

    client = LLMClient(
        SimpleNamespace(chat=SimpleNamespace(completions=_Hanging())),
        model_triage="nano",
        model_agent="mini",
    )
    d = TurnDeadline(total_ms=120, terminal_reserve_ms=20)
    token = deadline.push(d)
    try:
        with pytest.raises((TimeoutError, openai.APITimeoutError)):
            await client.complete("s", "u")
    finally:
        deadline.pop(token)
    assert _Hanging.calls >= 1
