"""Adaptor OpenAI (async) — SINGURUL loc care vorbește cu API-ul OpenAI.

Folosit de stagiile LLM (triaj nano, agent mini) și de jobul de embeddings.
Clientul `AsyncOpenAI` e injectabil → testele pasează un fake (zero apeluri reale
în CI, ca testele integration). Fără cheie configurată → `get_llm()` întoarce
`None`, iar pipeline-ul degradează grațios (echo), nu crapă (principiul 6).

LLM se apelează DOAR din stagiile triaj și agent (principiul 2) — adică prin
acest adaptor, niciodată direct din alt cod.
"""

import asyncio
import json
import logging
import random
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

import openai
from openai import AsyncOpenAI

from src.agent import tool_budget, usage
from src.config import get_settings
from src.observability import turn_latency
from src.runtime import deadline, turn_budget
from src.runtime.deadline import REASON_NO_ROOM, DeadlineExhausted

log = logging.getLogger(__name__)

# Erori TRANZITORII fără status HTTP (timeout / conexiune) — retry-abile.
_TRANSIENT_ERRORS = (openai.APITimeoutError, openai.APIConnectionError)

# NX-241 — plafonul de timp al apelurilor care NU sunt generare (clasificare/extracție). Sunt
# rapide prin natura lor: un moderation care durează 8s nu mai are pentru cine să modereze, iar
# gate-ul degradează fail-open oricum. Efectiv rămâne `min(cap, buget rămas − rezervă)`.
MODERATION_CAP_MS = 2_000


def _retry_after_seconds(exc: Exception) -> float | None:
    """Secundele din header-ul `Retry-After` (când providerul îl trimite), altfel None."""
    resp = getattr(exc, "response", None)
    headers = getattr(resp, "headers", None)
    if not headers:
        return None
    ra = headers.get("retry-after")
    try:
        return float(ra) if ra else None
    except (TypeError, ValueError):
        return None


async def _with_retry(
    factory: Callable[[], Awaitable[Any]], *, max_retries: int, cap_ms: int | None = None
) -> Any:
    """NX-126: retry bounded pe erori TRANZITORII (429 / 5xx / timeout / connection). Respectă
    `Retry-After` când există, altfel backoff exponențial cu jitter. 4xx terminale (400/401/403/404)
    ridică imediat (caller-ul degradează — P6). La epuizare loghează `llm_api_failure` și ridică.
    Trăiește DOAR în adaptor (cuplajul OpenAI stă la margine).

    NX-241 — cât timp există un `TurnDeadline` activ (flag ON), retry-ul nu mai e o buclă cu ceas
    propriu, ci un consumator al ACELUIAȘI buget:

      • fiecare încercare primește `min(cap_ms, remaining - rezervă terminală)`, deci un apel nu
        poate mânca timpul rezervat validatorului + commitului;
      • `Retry-After` se respectă DOAR dacă somnul + un minim util mai încap; altfel nu dormim
        degeaba, ridicăm și lăsăm apelantul să degradeze onest;
      • dacă nu mai e timp nici pentru prima încercare, nu pornim apelul deloc (`DeadlineExhausted`)
        — un apel pe care oricum îl anulăm costă bani și latență fără nicio șansă de rezultat.

    Fără deadline activ (default), comportamentul e byte-identic cu NX-126.
    """
    d = deadline.current()
    min_useful = getattr(get_settings(), "llm_retry_min_budget_ms", 600)
    delay = 0.5
    last: Exception | None = None
    for attempt in range(max_retries + 1):
        timeout_s = None if d is None else d.timeout_for(cap_ms)
        if timeout_s is not None and timeout_s <= 0:
            raise DeadlineExhausted("model", REASON_NO_ROOM)
        try:
            if timeout_s is None:
                return await factory()
            async with asyncio.timeout(timeout_s):
                return await factory()
        except openai.APIStatusError as e:
            # 429 (RateLimitError) + 5xx = tranzitoriu; restul 4xx = terminal → ridică.
            if e.status_code < 500 and not isinstance(e, openai.RateLimitError):
                raise
            last, wait = e, _retry_after_seconds(e)
        except _TRANSIENT_ERRORS as e:
            last, wait = e, None
        except TimeoutError as e:
            # Apel tăiat de PROPRIUL nostru deadline (`asyncio.timeout`). Fără deadline activ nu
            # există calea asta → ridicăm neatins, ca înainte.
            if d is None:
                raise
            last, wait = e, None
        if attempt >= max_retries:
            break
        sleep_s = (wait if wait is not None else delay) + random.uniform(0.0, 0.25)
        if d is not None and not d.fits(sleep_s * 1000.0, minimum_ms=min_useful):
            # 429 cu `Retry-After` peste ce a mai rămas: NU așteptăm. Degradăm terminal (P6).
            log.warning(
                "llm_api_failure: %s — retry abandonat, %0.2fs nu încap în bugetul rămas (%dms)",
                type(last).__name__,
                sleep_s,
                d.remaining_ms(),
            )
            turn_latency.degrade("llm_retry_no_budget")
            break
        log.warning(
            "llm_api_failure: %s tranzitoriu — retry %d/%d în %.2fs",
            type(last).__name__,
            attempt + 1,
            max_retries,
            sleep_s,
        )
        turn_latency.degrade("llm_retry")
        await asyncio.sleep(sleep_s)
        delay *= 2
    log.warning(
        "llm_api_failure: %s — epuizat după %d reîncercări", type(last).__name__, max_retries
    )
    raise last


async def _run_tool_calls(
    execute: Callable[[str, dict[str, Any]], Awaitable[str]], tool_calls: list[Any]
) -> list[str]:
    """Rulează tool-urile cerute într-o rundă și întoarce rezultatele ÎN ORDINEA CERUTĂ.

    Fără buget/deadline activ: `asyncio.gather` peste tot, exact ca înainte (byte-identic).

    Cu ele active (NX-241), runda se sparge în DOUĂ: întâi citirile independente, concurent (poarta
    din `ToolRun` le plafonează), apoi MUTAȚIILE, una câte una. Motivul e concret: o mutație
    lansată în același `gather` cu citirile ar putea scrie în timp ce încă citim starea pe care se
    bazează, iar două mutații în paralel sunt exact felul în care se dublează un coș. Ordinea
    rezultatelor rămâne cea a apelurilor — modelul primește un răspuns determinist, nu ordinea în
    care s-a întâmplat să se termine tool-urile.
    """
    calls = [(tc.function.name, _parse_args(tc.function.arguments)) for tc in tool_calls]
    if deadline.current() is None and turn_budget.current() is None:
        return list(await asyncio.gather(*(execute(name, args) for name, args in calls)))

    results: list[str] = [""] * len(calls)
    reads = [i for i, (name, _) in enumerate(calls) if not tool_budget.spec_for(name).is_mutation]
    read_set = set(reads)
    mutations = [i for i in range(len(calls)) if i not in read_set]
    if reads:
        done = await asyncio.gather(*(execute(*calls[i]) for i in reads))
        for i, content in zip(reads, done, strict=True):
            results[i] = content
    for i in mutations:  # seriale, în ordinea cerută de model
        results[i] = await execute(*calls[i])
    return results


def _usage_snapshot() -> tuple[int, int, int, int, float] | None:
    """Fotografia acumulatorului de usage (sau None în afara unui tur) — NX-241 o diff-uiește ca să
    scadă tokenii/costul RUNDEI din bugetul turului, la sursă, nu la sfârșit."""
    acc = usage.current()
    return None if acc is None else acc.snapshot()


def _charge_usage(before: tuple[int, int, int, int, float] | None) -> None:
    """Scade din buget ce a costat runda. Post-factum prin natura lucrurilor (tokenii se știu abia
    din răspuns): NUMĂRĂ, nu refuză — refuzul e la runda următoare, care vede plafonul atins."""
    acc = usage.current()
    if acc is None or before is None:
        return
    after = acc.snapshot()
    turn_budget.consume("tokens", (after[1] - before[1]) + (after[2] - before[2]))
    turn_budget.consume("cost_usd", after[4] - before[4])


def _round_admitted() -> bool:
    """Mai am voie la o rundă de model? Fără ledger (flag stins) → mereu DA."""
    if not turn_budget.reserve("model_rounds"):
        log.info("llm: rundă de model refuzată de buget — forțez răspunsul final")
        return False
    ledger = turn_budget.current()
    if ledger is not None and ledger.enforced and ledger.exhausted("cost_usd"):
        log.warning("llm: plafon de cost atins — forțez răspunsul final")
        return False
    return True


def _parse_args(raw: str | None) -> dict[str, Any]:
    """Argumentele unui tool_call (JSON string de la model) → dict. {} la JSON invalid
    (Pydantic din tool respinge restul)."""
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, dict) else {}
    except (ValueError, TypeError):
        return {}


@dataclass(frozen=True)
class ModerationResult:
    """Rezultatul moderation-ului (NX-15). `categories` = doar categoriile True
    (ex. ['harassment', 'hate']) — fără corpul mesajului (principiul 12)."""

    flagged: bool
    categories: list[str]


# Vision (NX-76): sentinel pentru o poză care NU e produs (selfie/screenshot/peisaj). Definit AICI
# și interpolat în prompt → codul (gates._route_image) match-uiește exact ce cere promptul, fără
# drift între cele două. La match → fail-soft determinist (clarificare), nu căutare pe text mort.
VISION_NOT_PRODUCT = "nu pare un produs"

# Vision: extractor vizual, NU vânzător. Cere STRICT atribute observabile (tip produs, brand
# vizibil, culoare, text de pe etichetă) ca interogare de căutare; interzice inventarea de
# preț/disponibilitate (groundarea rămâne treaba search-ului + validatorului din agent).
_VISION_SYSTEM = (
    "Ești un extractor vizual pentru un asistent de vânzări. Primești poza unui produs trimisă "
    "de un client. Descrie-l STRICT ca o interogare scurtă de căutare în catalog: tip de produs, "
    "brand vizibil pe ambalaj, culoare, și text citibil de pe etichetă. Răspunde cu o singură "
    "frază (max ~15 cuvinte), în limba română, fără introduceri sau ghilimele. NU inventa preț, "
    f"disponibilitate sau detalii pe care NU le vezi în poză. Dacă nu pare un produs (selfie, "
    f"captură de ecran, peisaj), răspunde exact: {VISION_NOT_PRODUCT}."
)


# ── Ce acceptă un model NU e o preferință a noastră, e o proprietate a LUI ────────────────────
# `temperature` și `reasoning_effort` sunt singurele opționale pe care le adăugăm noi peste
# `chat.completions` (vezi `_sampling`). MĂSURAT pe API-ul real (2026-08-24, `gpt-5.6-luna` și
# `gpt-5.6-terra`): familia `gpt-5.6-*` REFUZĂ orice `temperature` ≠ 1 cu
# `400 unsupported_value: 'temperature' does not support 0.7 with this model`; `gpt-5.4-*` o
# acceptă; `reasoning_effort` e acceptat de toate patru.
#
# De ce e o poartă și nu o notă în docs: opționalele astea pleacă DOAR pe apelurile care trec prin
# `_chat`, iar `_with_retry` tratează 4xx non-429 ca TERMINAL. Un model prins pe picior greșit nu
# degradează, ci omoară exact calea pe care e trimis — și numai pe aia, deci restul sistemului pare
# sănătos. S-a întâmplat de două ori: `max_tokens` pe `gpt-5.4-*` (PR #133) și `temperature` pe
# Luna, când `bbb77b3` a promovat default-ul `gpt-5.4-mini` → `gpt-5.6-luna` (24 aug) și fiecare
# tur de vânzare a căzut pe fallback-ul de runner, cu triajul (nano) intact deasupra.
#
# Necunoscutul se tratează în direcția SIGURĂ: prefix nedeclarat ⇒ niciun opțional. Costul unei
# omisiuni greșite e variație de copy pierdută (corectitudinea o ține validatorul stagiului 8);
# costul unui parametru greșit e turul pierdut. Nu sunt comparabile, deci nu se echilibrează.
_PARAM_SUPPORT: tuple[tuple[str, frozenset[str]], ...] = (
    ("gpt-5.6-", frozenset({"reasoning_effort"})),
    ("gpt-5.4-", frozenset({"reasoning_effort", "temperature"})),
)


def supported_params(model: str) -> frozenset[str]:
    """Opționalele pe care le acceptă `model`. Prefix nedeclarat → niciunul (fail-safe)."""
    for prefix, params in _PARAM_SUPPORT:
        if model.startswith(prefix):
            return params
    return frozenset()


def _note_truncation(resp: Any, *, cap: int | None) -> None:
    """Numără apelurile oprite de plafonul de output (`finish_reason == "length"`).

    Tokenii de RAȚIONAMENT se scad din același `max_completion_tokens` ca textul (măsurat: cu
    `reasoning_effort=high` și cap 16, `gpt-5.4-mini` consumă tot capul pe raționament și întoarce
    conținut GOL, cu `finish_reason="length"` și status 200). Un răspuns gol nu ridică nimic: iese
    ca proză vidă și devine fallback determinist, deci cauza reală (plafon prea mic pentru effortul
    cerut) nu apare nicăieri. E doar OBSERVABILITATE — nu schimbă turul (P6, P10) — dar transformă
    un non-răspuns tăcut într-o degradare numărată."""
    try:
        choice = (getattr(resp, "choices", None) or [None])[0]
        if choice is None or getattr(choice, "finish_reason", None) != "length":
            return
        empty = not (getattr(choice.message, "content", None) or "").strip()
        turn_latency.degrade("llm_output_truncated_empty" if empty else "llm_output_truncated")
        log.warning(
            "llm: completion oprit de plafon (cap=%s, conținut gol=%s) — tokenii de raționament "
            "intră în ACELAȘI buget ca textul",
            cap,
            empty,
        )
    except Exception:  # noqa: BLE001 — observabilitatea nu are voie să rupă un apel reușit (P6)
        return


class LLMClient:
    """Wrapper subțire peste AsyncOpenAI. Modelele vin din settings (nano/mini)."""

    def __init__(
        self,
        client: AsyncOpenAI,
        *,
        model_triage: str,
        model_agent: str,
        model_embed: str = "text-embedding-3-small",
        model_moderation: str = "omni-moderation-latest",
        model_vision: str = "gpt-5.6-terra",
    ) -> None:
        self._client = client
        self.model_triage = model_triage
        self.model_agent = model_agent
        self.model_embed = model_embed
        self.model_moderation = model_moderation
        self.model_vision = model_vision

    def _sampling(self, *, agent: bool, model: str) -> dict[str, Any]:
        """Params trimiși la chat.completions, pe TREI axe:

        • Plafon de output: `max_completion_tokens` pe TOATE apelurile de agent, MEREU (NX-125 — un
          completion patologic/buclă nu scapă de ceiling), independent de sampling. Folosim
          `max_completion_tokens`, NU `max_tokens` (deprecat → 400 pe modelele curente gpt-5.4-*).
          E acceptat de toate familiile, deci nu trece prin poarta de capabilitate.
        • `temperature`: gated de `llm_sampling_enabled` ȘI de ce acceptă modelul. ON → variație
          controlată (agent: copy ne-repetitiv `llm_temperature_agent`; triaj: clasificare
          deterministă `llm_temperature_triage`). OFF → omis → default-ul modelului. Corectitudinea
          NU depinde de temperatură (o asigură validatorul stagiului 8), deci `agent` poate fi urcat
          liber — dar pe `gpt-5.6-*` valoarea nu pleacă deloc, fiindcă modelul o refuză cu 400.
        • `reasoning_effort`: gated de `llm_reasoning_effort_agent` ȘI de ce acceptă modelul.

        Axele NU sunt independente de model: `supported_params` decide ce are voie să plece, ca un
        default de model schimbat să nu mai poată omorî o cale întreagă (vezi comentariul de la
        `_PARAM_SUPPORT`). Un opțional lăsat acasă se numără ca degradare, ca să nu dispară tăcut
        dintr-un tur pe care îl credem configurat.

        Triajul (agent=False) nu primește ceiling (JSON scurt). embed/moderate/vision nu trec
        pe aici."""
        s = get_settings()
        supported = supported_params(model)
        out: dict[str, Any] = {}
        if agent:
            out["max_completion_tokens"] = s.llm_max_tokens_agent
            if effort := (getattr(s, "llm_reasoning_effort_agent", "") or "").strip():
                if "reasoning_effort" in supported:
                    out["reasoning_effort"] = effort
                else:
                    turn_latency.degrade("llm_param_unsupported_reasoning_effort")
        if s.llm_sampling_enabled:
            if "temperature" in supported:
                out["temperature"] = s.llm_temperature_agent if agent else s.llm_temperature_triage
            else:
                turn_latency.degrade("llm_param_unsupported_temperature")
        return out

    async def _chat(self, *, agent: bool, **kwargs: Any):
        """Wrapper unic pe chat.completions.create: retry bounded (NX-126) + sampling params.

        NX-241: plafonul de timp al UNUI apel (`llm_call_cap_ms`) intră aici, nu în fiecare
        apelant — timeoutul efectiv rămâne `min(cap, buget rămas − rezervă)`."""
        kwargs.update(self._sampling(agent=agent, model=kwargs["model"]))
        s = get_settings()
        resp = await _with_retry(
            lambda: self._client.chat.completions.create(**kwargs),
            max_retries=s.llm_retry_max,
            cap_ms=getattr(s, "llm_call_cap_ms", 8_000),
        )
        _note_truncation(resp, cap=kwargs.get("max_completion_tokens"))
        return resp

    async def classify_json(self, system: str, user: str, *, model: str | None = None) -> dict:
        """Apel chat cu răspuns JSON forțat (`response_format=json_object`).

        Întoarce dict-ul parsat. Folosit de triaj (clasificare rută). Modelul
        implicit e cel de triaj (nano). Ridică la JSON invalid / eroare de API —
        caller-ul (stagiul) prinde și degradează."""
        mdl = model or self.model_triage
        resp = await self._chat(
            agent=False,
            model=mdl,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            response_format={"type": "json_object"},
        )
        usage.record_chat(resp, mdl)
        content = resp.choices[0].message.content or "{}"
        return json.loads(content)

    async def complete_schema(
        self, system: str, user: str, schema: dict[str, Any], *, model: str | None = None
    ) -> dict:
        """Apel chat cu STRUCTURED OUTPUT strict (`response_format=json_schema`). Modelul
        e forțat să întoarcă JSON conform `schema` (= {name, strict, schema}). Folosit de
        agent pentru recomandarea structurată (model iZi): modelul emite DOAR cuvinte +
        referințe product_id, niciun preț/link. Modelul implicit = agent (mini), care deja
        depinde de `strict:true` în tool-uri. Ridică la JSON invalid / eroare API — caller
        prinde și degradează pe calea de proză liberă."""
        mdl = model or self.model_agent
        resp = await self._chat(
            agent=True,
            model=mdl,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            response_format={"type": "json_schema", "json_schema": schema},
        )
        usage.record_chat(resp, mdl)
        content = resp.choices[0].message.content or "{}"
        return json.loads(content)

    async def complete(self, system: str, user: str, *, model: str | None = None) -> str:
        """Apel chat care întoarce TEXT simplu (nu JSON). Modelul implicit = agent
        (mini). Folosit de agent pentru a compune recomandarea. Ridică la eroare de
        API — caller-ul prinde și degradează."""
        mdl = model or self.model_agent
        resp = await self._chat(
            agent=True,
            model=mdl,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        )
        usage.record_chat(resp, mdl)
        return (resp.choices[0].message.content or "").strip()

    async def run_tool_loop(
        self,
        system: str,
        user: str,
        tools: list[dict[str, Any]],
        execute: Callable[[str, dict[str, Any]], Awaitable[str]],
        *,
        max_steps: int = 3,
        model: str | None = None,
    ) -> str:
        """Buclă de tool-calling (agentul, G7). Modelul cere tool-uri → `execute(name, args)`
        le rulează (callback-ul agentului, întoarce `llm_view`) → rezultatele intră înapoi în
        conversație → repetă. CAP DUR `max_steps` (CLAUDE.md: max 3 tool calls/tur); la atingere
        forțează un text final FĂRĂ tools. Formatul OpenAI (tool_calls / rol `tool`) stă DOAR
        aici (adaptorul = singurul loc care vorbește OpenAI). Întoarce textul final.

        `execute` poate fi chemat de mai multe ori într-un pas (modelul cere ≥1 tool) — le
        rulăm CONCURENT (`asyncio.gather`) ca să tăiem latența."""
        mdl = model or self.model_agent
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ]
        for _ in range(max_steps):
            if not _round_admitted():
                break  # buget de runde/cost atins → ieșim la apelul final FĂRĂ tools (text forțat)
            before = _usage_snapshot()
            with turn_latency.span("model"):
                resp = await self._chat(
                    agent=True, model=mdl, messages=messages, tools=tools, tool_choice="auto"
                )
            usage.record_chat(resp, mdl)
            _charge_usage(before)
            msg = resp.choices[0].message
            tool_calls = getattr(msg, "tool_calls", None)
            if not tool_calls:
                return (msg.content or "").strip()
            messages.append(
                {
                    "role": "assistant",
                    "content": msg.content or "",
                    "tool_calls": [
                        {
                            "id": tc.id,
                            "type": "function",
                            "function": {
                                "name": tc.function.name,
                                "arguments": tc.function.arguments,
                            },
                        }
                        for tc in tool_calls
                    ],
                }
            )
            contents = await _run_tool_calls(execute, tool_calls)
            for tc, content in zip(tool_calls, contents, strict=True):
                messages.append({"role": "tool", "tool_call_id": tc.id, "content": content})

        # cap atins → un ultim apel FĂRĂ tools (text forțat, nu o a 4-a rundă de tool calls).
        before = _usage_snapshot()
        with turn_latency.span("model"):
            resp = await self._chat(agent=True, model=mdl, messages=messages)
        usage.record_chat(resp, mdl)
        _charge_usage(before)
        return (resp.choices[0].message.content or "").strip()

    async def run_tool_loop_structured(
        self,
        system: str,
        user: str,
        tools: list[dict[str, Any]],
        execute: Callable[[str, dict[str, Any]], Awaitable[str]],
        schema: dict[str, Any],
        *,
        max_steps: int = 3,
        model: str | None = None,
    ) -> tuple[dict[str, Any], int]:
        """NX-239 — aceeași buclă de tool-calling, dar răspunsul FINAL al ACELUIAȘI model este
        un obiect STRUCTURAT (`response_format=json_schema`), nu proză: planul iese din bucla în
        care s-au văzut tool results, fără un writer separat care să „rescrie" răspunsul.

        `response_format` e setat pe TOATE apelurile: când modelul nu mai cere tool-uri, corpul
        mesajului E planul. La cap atins, un ultim apel FĂRĂ tools forțează planul. Întoarce
        `(dict-ul parsat, runde_de_tool)`. Ridică la JSON invalid / eroare API — caller-ul
        (brain-ul) face UN repair bounded și apoi fallback determinist. Model-agnostic: `model`
        vine din settings, nu e hardcodat aici."""
        mdl = model or self.model_agent
        response_format = {"type": "json_schema", "json_schema": schema}
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ]
        rounds = 0
        for _ in range(max_steps):
            if not _round_admitted():
                break  # NX-241: plafonul de runde/cost e al CODULUI, nu al modelului
            before = _usage_snapshot()
            with turn_latency.span("model"):
                resp = await self._chat(
                    agent=True,
                    model=mdl,
                    messages=messages,
                    tools=tools,
                    tool_choice="auto",
                    response_format=response_format,
                )
            usage.record_chat(resp, mdl)
            _charge_usage(before)
            msg = resp.choices[0].message
            tool_calls = getattr(msg, "tool_calls", None)
            if not tool_calls:
                return json.loads(msg.content or "{}"), rounds
            rounds += 1
            messages.append(
                {
                    "role": "assistant",
                    "content": msg.content or "",
                    "tool_calls": [
                        {
                            "id": tc.id,
                            "type": "function",
                            "function": {
                                "name": tc.function.name,
                                "arguments": tc.function.arguments,
                            },
                        }
                        for tc in tool_calls
                    ],
                }
            )
            contents = await _run_tool_calls(execute, tool_calls)
            for tc, content in zip(tool_calls, contents, strict=True):
                messages.append({"role": "tool", "tool_call_id": tc.id, "content": content})

        before = _usage_snapshot()
        with turn_latency.span("model"):
            resp = await self._chat(
                agent=True, model=mdl, messages=messages, response_format=response_format
            )
        usage.record_chat(resp, mdl)
        _charge_usage(before)
        return json.loads(resp.choices[0].message.content or "{}"), rounds

    async def moderate(self, text: str, *, model: str | None = None) -> ModerationResult:
        """Clasifică un mesaj cu endpointul de moderation OpenAI (gratuit, NU generare —
        principiul 2, ca embed). Folosit de Gates (NX-15) ÎNAINTE de triaj. Ridică la
        eroare de API — caller-ul (gate) prinde și degradează fail-open."""
        resp = await _with_retry(
            lambda: self._client.moderations.create(
                model=model or self.model_moderation, input=text
            ),
            max_retries=get_settings().llm_retry_max,
            cap_ms=MODERATION_CAP_MS,
        )
        r = resp.results[0]
        data = r.categories.model_dump()
        flagged = [k for k, v in data.items() if v]
        return ModerationResult(flagged=bool(r.flagged), categories=sorted(flagged))

    async def describe_image(self, image_b64: str, mime: str, *, model: str | None = None) -> str:
        """Descrie o poză de produs ca TEXT de căutare în catalog (Vision, NX-76). Extracție, NU
        generare/conversație — în spiritul `embed`/`moderate` (principiul 2). `detail:"low"` +
        `max_tokens` mic = costul tăiat în cod (un tile, fără high-res). Modelul implicit are
        vedere (mini). Ridică la eroare de API — caller-ul (gate) prinde și degradează fail-soft."""
        mdl = model or self.model_vision
        # Vision: NU trecem prin `_chat` (fără `temperature` — extracție, nu generare). Doar retry.
        resp = await _with_retry(
            lambda: self._client.chat.completions.create(
                model=mdl,
                messages=[
                    {"role": "system", "content": _VISION_SYSTEM},
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "text",
                                "text": "Descrie produsul din poză ca interogare de căutare.",
                            },
                            {
                                "type": "image_url",
                                "image_url": {
                                    "url": f"data:{mime};base64,{image_b64}",
                                    "detail": "low",
                                },
                            },
                        ],
                    },
                ],
                # max_completion_tokens (NU max_tokens, deprecat → 400 pe gpt-5.4-*); 256 = headroom
                # pentru tokenii de „reasoning" + extracția scurtă, cost tot mic (detail:"low").
                max_completion_tokens=256,
            ),
            max_retries=get_settings().llm_retry_max,
            cap_ms=getattr(get_settings(), "llm_call_cap_ms", 8_000),
        )
        usage.record_chat(resp, mdl)
        return (resp.choices[0].message.content or "").strip()

    async def embed(self, texts: list[str], *, model: str | None = None) -> list[list[float]]:
        """Embeddings pentru un lot de texte. Întoarce o listă de vectori (1536 dim
        la text-embedding-3-small). Folosit de jobul `embed_products` + (viitor)
        cache semantic / search semantic."""
        mdl = model or self.model_embed
        s = get_settings()
        resp = await _with_retry(
            lambda: self._client.embeddings.create(model=mdl, input=texts),
            max_retries=s.llm_retry_max,
            # `embed_timeout_ms` (NX-225) rămâne plafonul embedului; deadline-ul turului îl poate
            # doar STRÂNGE, niciodată lărgi. 0 = fără plafon propriu → doar bugetul turului.
            cap_ms=s.embed_timeout_ms or None,
        )
        usage.record_embeddings(resp, mdl)
        return [d.embedding for d in resp.data]


_llm: LLMClient | None = None


def get_llm() -> LLMClient | None:
    """Singleton per proces. `None` dacă nu e cheie OpenAI (degradare grațioasă)."""
    global _llm
    if _llm is None:
        s = get_settings()
        if not s.openai_api_key:
            return None
        # NX-126: timeout anti-hang; max_retries=0 dezactivează retry-ul intern al SDK-ului
        # (folosim `_with_retry` ca să controlăm backoff-ul + logul `llm_api_failure`).
        _llm = LLMClient(
            AsyncOpenAI(api_key=s.openai_api_key, timeout=s.llm_timeout_s, max_retries=0),
            model_triage=s.model_triage,
            model_agent=s.model_agent,
            model_embed=s.model_embed,
            model_moderation=s.model_moderation,
            model_vision=s.model_vision,
        )
    return _llm
