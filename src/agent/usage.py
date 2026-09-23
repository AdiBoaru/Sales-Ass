"""Captarea usage-ului LLM per tur (tokeni + cost + cache hits) — observabilitate de cost.

Problema: răspunsurile OpenAI poartă `usage` (prompt/completion/cached tokens), dar adaptorul
nu le citea → `usage_daily.tokens_in/out/cost_usd` erau mereu 0, iar economia din prompt caching
(NX-78) invizibilă. Soluția respectă principiul 10 (stagiile nu știu că sunt măsurate):

  • adaptorul (`src.agent.llm`, singurul care vorbește OpenAI) raportează usage-ul fiecărui apel
    aici, prin `record_chat` / `record_embeddings`;
  • runner-ul deschide un acumulator per tur (`push`/`pop`) și emite UN event `llm_usage` la final;
  • processor-ul deschide un al doilea acumulator în jurul apelurilor POST-tur (summarizer / profil)
    → un al doilea `llm_usage` (phase=post_turn), ca nimic să nu scape rollup-ului (NX-103).

Izolare la concurență: acumulatorul stă într-un `ContextVar`. `asyncio.gather` (ex. tool-urile
rulate în paralel în `run_tool_loop`) copiază contextul, dar TOATE copiile văd ACEEAȘI instanță
(o mutăm, nu re-legăm var-ul) → tokenii sub-apelurilor concurente se adună corect, fără să se
amestece între tururi.
"""

from __future__ import annotations

import contextvars
from dataclasses import dataclass, field
from typing import Any

from src.agent.pricing import cost_for

#: NX-312 — plafonul listei de apeluri per tur (P4). Un tur de recomandare face azi 3 apeluri de
#: chat, iar plafonul de runde (3) plus compunerea și un retry de recompunere duc maximul legitim pe
#: la 6. Peste 8 nu mai e un tur, e o buclă — se numără în `per_call_dropped`, nu se aruncă tăcut.
MAX_CALL_ROWS = 8

#: Forma unui apel, derivată din CEREREA care pleacă pe sârmă (vezi `request_shape`). Vocabular
#: ÎNCHIS: `tools` = rundă de tool-calling, `schema` = răspuns JSON forțat (json_schema SAU
#: json_object), `text` = răspuns liber.
CALL_SHAPES = ("tools", "schema", "text")


def request_shape(kwargs: dict[str, Any]) -> str:
    """Forma unui apel de chat, din argumentele lui. PUR.

    Derivată, nu pasată de apelant (P10: stagiile nu știu că sunt măsurate), și din ACEEAȘI
    proprietate care decide legalitatea cererii: un apel cu `tools` nu poate raționa pe
    `chat.completions`, deci forma e și cea care separă cele două populații de durată (NX-311).
    `tools` bate `response_format`: bucla structurată a creierului unic le trimite pe amândouă, iar
    ce o face lentă sau rapidă e bitul de raționament, pe care îl dictează uneltele."""
    if kwargs.get("tools"):
        return "tools"
    if kwargs.get("response_format"):
        return "schema"
    return "text"


def _empty_model_row() -> dict[str, Any]:
    return {
        "calls": 0,
        "tokens_in": 0,
        "tokens_out": 0,
        "cached_tokens": 0,
        "reasoning_tokens": 0,
        "cost_usd": 0.0,
    }


@dataclass
class UsageAccumulator:
    """Totalurile unui tur. `tokens_in` = prompt (INCLUDE `cached_tokens`); `tokens_out` =
    completion (INCLUDE `reasoning_tokens`). `cost_usd` = sumă pe apeluri, cu tokenii cached la
    tarif redus. `by_model` = defalcare per model (nano/mini/embeddings) pentru raportul de cost
    (NX-103).

    Ambele câmpuri „details" sunt SUBSETURI, nu suplimente: `cached_tokens ⊆ tokens_in`,
    `reasoning_tokens ⊆ tokens_out`. Nu le aduna la totaluri și nu le factura separat — costul e
    deja calculat pe prompt/completion. Regula stă aici fiindcă ăsta e obiectul pe care îl citește
    oricine adaugă un raport; vezi `_reasoning_from` pentru de ce contează defalcarea.

    `snapshot()` NU include raționamentul, deliberat: NX-241 îl diff-uiește ca să scadă consumul
    rundei din bugetul turului, iar un subset adăugat acolo ar taxa de două ori aceiași tokeni."""

    calls: int = 0
    tokens_in: int = 0
    tokens_out: int = 0
    cached_tokens: int = 0
    reasoning_tokens: int = 0
    cost_usd: float = 0.0
    by_model: dict[str, dict[str, Any]] = field(default_factory=dict)
    # NX-312: un rând per apel de chat, în ordine. Totalurile de mai sus spun CÂT a costat turul;
    # lista spune CARE apel a costat. Pe turul `d73822a3` totalul era `model n=3, 32.437 ms`, iar
    # împărțirea 3s + 3s + 26s se putea doar deduce prin scădere.
    call_rows: list[dict[str, Any]] = field(default_factory=list)
    call_rows_dropped: int = 0

    def add_call_row(self, row: dict[str, Any]) -> None:
        if len(self.call_rows) < MAX_CALL_ROWS:
            self.call_rows.append(row)
        else:
            self.call_rows_dropped += 1

    def add(
        self, model: str, prompt: int, completion: int, cached: int, *, reasoning: int = 0
    ) -> None:
        cost = cost_for(model, prompt, cached, completion)
        self.calls += 1
        self.tokens_in += prompt
        self.tokens_out += completion
        self.cached_tokens += cached
        self.reasoning_tokens += reasoning
        self.cost_usd += cost
        row = self.by_model.setdefault(model, _empty_model_row())
        row["calls"] += 1
        row["tokens_in"] += prompt
        row["tokens_out"] += completion
        row["cached_tokens"] += cached
        row["reasoning_tokens"] += reasoning
        row["cost_usd"] += cost

    def snapshot(self) -> tuple[int, int, int, int, float]:
        """Stare curentă (calls, in, out, cached, cost) — runner-ul o diff-uiește per stagiu."""
        return (self.calls, self.tokens_in, self.tokens_out, self.cached_tokens, self.cost_usd)


_current: contextvars.ContextVar[UsageAccumulator | None] = contextvars.ContextVar(
    "llm_usage", default=None
)


def push() -> tuple[UsageAccumulator, contextvars.Token]:
    """Deschide un acumulator nou pentru turul curent. Întoarce (acc, token) → `pop(token)`."""
    acc = UsageAccumulator()
    return acc, _current.set(acc)


def pop(token: contextvars.Token) -> None:
    """Închide capturarea (restaurează valoarea anterioară a ContextVar-ului)."""
    _current.reset(token)


def current() -> "UsageAccumulator | None":
    """Acumulatorul turului curent, sau `None` (în afara unui tur). NX-241 îl diff-uiește în jurul
    unei runde de model ca să scadă tokenii/costul din bugetul turului, la sursă."""
    return _current.get()


def _cached_from(usage: Any) -> int:
    """`prompt_tokens_details.cached_tokens` — tolerează obiect SDK SAU dict SAU lipsă."""
    details = getattr(usage, "prompt_tokens_details", None)
    if details is None and isinstance(usage, dict):
        details = usage.get("prompt_tokens_details")
    if details is None:
        return 0
    if isinstance(details, dict):
        return int(details.get("cached_tokens") or 0)
    return int(getattr(details, "cached_tokens", 0) or 0)


def cached_tokens_of(resp: Any) -> int:
    """Tokenii de prompt serviți din cache pe UN răspuns (NX-275). Tolerant ca `_cached_from`.

    Public fiindcă îl citește și `llm._chat`, ca să pună cifra pe spanul apelului: acolo se vede
    ce nu se vede în totalul pe tur — apelul 1 SCRIE cache-ul, abia apelul 2 îl citește."""
    usage = getattr(resp, "usage", None)
    return _cached_from(usage) if usage is not None else 0


def _reasoning_from(usage: Any) -> int | None:
    """`completion_tokens_details.reasoning_tokens` — obiect SDK SAU dict. `None` = NERAPORTAT.

    ATENȚIE, e un SUBSET al lui `completion_tokens`, nu un plus: tokenii de raționament se scad din
    ACELAȘI `max_completion_tokens` ca textul (măsurat — vezi `llm._note_truncation`). Deci NU intră
    în cost separat (ar dubla factura) și nu se adună la `tokens_out`. Îl ținem ca defalcare: fără
    el nu poți răspunde la „cât din plafon a mâncat raționamentul", adică exact întrebarea care
    decide dacă `LLM_MAX_TOKENS_AGENT` e dimensionat corect pentru effortul configurat.

    De ce `None` și nu `0`: ăsta e un INSTRUMENT DE MĂSURĂ, iar într-un instrument degradarea
    grațioasă e o minciună. „Câmp absent" (nu știm) și „zero tokeni de gândire" (știm, și e zero)
    sunt afirmații opuse, dar arată identic dacă le colapsezi în `0` — iar cea greșită e liniștitor
    de plauzibilă: te-ai uita la un raport care spune „raționamentul nu costă nimic" și ai închide
    ancheta. Distincția o folosește `record_chat`, care numără cazul nedeclarat."""
    details = getattr(usage, "completion_tokens_details", None)
    if details is None and isinstance(usage, dict):
        details = usage.get("completion_tokens_details")
    if details is None:
        return None
    raw = (
        details.get("reasoning_tokens")
        if isinstance(details, dict)
        else getattr(details, "reasoning_tokens", None)
    )
    return None if raw is None else int(raw)


def _note_unreported_reasoning(model: str) -> None:
    """Numără apelurile pe un model care RAȚIONEAZĂ dar n-a raportat `reasoning_tokens`.

    Poarta e pe familia modelului, nu pe toate apelurile: `gpt-5.4-nano` fără effort chiar nu are
    ce raporta, deci a-l număra ar produce un contor mereu aprins, adică zgomot care se învață să
    fie ignorat. Pe o familie care raționează implicit (`reasons_by_default`), absența câmpului
    înseamnă ori că furnizorul l-a redenumit, ori că citim greșit — în ambele cazuri raportul de
    mai sus arată `0` și pare o măsurătoare reușită. Contorul e singura diferență între un
    instrument și o minciună liniștitoare.

    Importuri locale: `llm` importă `usage`, deci la nivel de modul ar fi ciclu (tiparul din
    `model_role`); `turn_latency` la fel ca `hooks` — legat la primul apel real, nu la încărcarea
    modulului."""
    from src.agent.llm import model_profile
    from src.observability import turn_latency

    try:
        profile = model_profile(model)
    except Exception:  # noqa: BLE001 — o măsurătoare nu are voie să rupă un apel reușit (P6)
        return
    if profile is not None and profile.reasons_by_default:
        turn_latency.degrade("llm_reasoning_tokens_unreported")


def _field(usage: Any, name: str) -> int:
    val = usage.get(name) if isinstance(usage, dict) else getattr(usage, name, 0)
    return int(val or 0)


def record_chat(resp: Any, model: str) -> None:
    """Raportează usage-ul unui apel chat (best-effort). Fără acumulator activ sau fără `usage`
    pe răspuns (ex. fake-uri din teste) → no-op, nu rupe turul."""
    acc = _current.get()
    if acc is None:
        return
    usage = getattr(resp, "usage", None)
    if usage is None:
        return
    tokens_in = _field(usage, "prompt_tokens")
    tokens_out = _field(usage, "completion_tokens")
    cached = _cached_from(usage)
    reasoning = _reasoning_from(usage)
    if reasoning is None:
        _note_unreported_reasoning(model)
    acc.add(model, tokens_in, tokens_out, cached, reasoning=reasoning or 0)
    # NX-246: același punct unic de contabilizare alimentează și metricile operaționale. Hook
    # NEUTRU (`src/observability/hooks.py`) — adaptorul nu știe de exporter, nu decide sampling
    # și nu importă niciun vendor. Rolul se DERIVĂ din model (vezi `model_role`), ca să nu
    # schimbăm semnătura pe toate căile de apel pentru o etichetă.
    _record_model_metrics(model, tokens_in, tokens_out, cached)


def record_call(resp: Any, *, shape: str, reasoning: bool, ms: float, ok: bool) -> None:
    """NX-312 — un rând per apel de chat, cu durata și tokenii LUI (best-effort, ca `record_chat`).

    Chemat din `llm._chat`, wrapperul unic al tuturor apelurilor, deci și apelurile EȘUATE intră
    în listă (`ok=False`, tokeni 0): un apel care moare după 90s de retry e exact rândul pe care
    îl cauți când turul a durat două minute.

    `ms` include retry-urile și backoff-ul: e durata apelului așa cum a trăit-o TURUL, nu a unei
    încercări. `reasoning_tokens` rămâne `None` când furnizorul nu-l raportează — instrument de
    măsură, deci „nu știm" nu se colapsează în `0` (vezi `_reasoning_from`).

    Numai numere și un cuvânt din vocabularul închis: zero text, zero identificatori (P12)."""
    acc = _current.get()
    if acc is None:
        return
    usage = getattr(resp, "usage", None) if resp is not None else None
    acc.add_call_row(
        {
            "shape": shape if shape in CALL_SHAPES else "text",
            "reasoning": bool(reasoning),
            "ok": bool(ok),
            "ms": round(float(ms), 1),
            "tokens_in": _field(usage, "prompt_tokens") if usage is not None else 0,
            "cached": _cached_from(usage) if usage is not None else 0,
            "tokens_out": _field(usage, "completion_tokens") if usage is not None else 0,
            "reasoning_tokens": _reasoning_from(usage) if usage is not None else None,
        }
    )


def model_role(model: str) -> str:
    """`model_id` → rol (`triage|agent|embed|vision|moderation`), din settings.

    Derivat, nu pasat: rolul e o proprietate a CONFIGURAȚIEI, iar a-l căra prin toate semnăturile
    de apel doar ca să-l punem pe o etichetă ar fi cuplaj plătit degeaba. Necunoscut → `agent`
    (calea implicită), nu o valoare nouă care ar deschide cardinalitate.
    """
    from src.config import get_settings

    try:
        s = get_settings()
    except Exception:  # noqa: BLE001 — fără settings (script/test parțial) nu ghicim
        return "agent"
    for role, attr in (
        ("embed", "model_embed"),
        ("vision", "model_vision"),
        ("moderation", "model_moderation"),
    ):
        if model and model == getattr(s, attr, None):
            return role
    return "agent"


def _record_model_metrics(model: str, tokens_in: int, tokens_out: int, cached: int) -> None:
    # Import local: `hooks` → `metrics` are o poartă de contract la import, iar `usage` e importat
    # de tot lanțul de agent. Legarea se face la primul apel real, nu la încărcarea modulului.
    from src.observability import hooks

    try:
        # ACEEAȘI formulă ca `UsageAccumulator.add` (ordinea argumentelor contează: prompt, cached,
        # completion). Două formule de cost care diverg ar face ca metrica și `usage_daily` să
        # spună lucruri diferite despre aceeași factură.
        cost = cost_for(model, tokens_in, cached, tokens_out)
    except Exception:  # noqa: BLE001 — un model fără tarif nu are voie să rupă contabilizarea
        cost = 0.0
    hooks.on_model_call(
        model,
        model_role=model_role(model),
        tokens_in=tokens_in,
        tokens_out=tokens_out,
        cached_tokens=cached,
        cost_usd=cost,
    )


def record_embeddings(resp: Any, model: str) -> None:
    """Raportează usage-ul unui apel de embeddings (doar prompt tokens; fără cached/output)."""
    acc = _current.get()
    if acc is None:
        return
    usage = getattr(resp, "usage", None)
    if usage is None:
        return
    acc.add(model, _field(usage, "prompt_tokens"), 0, 0)
