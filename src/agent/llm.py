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
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from time import perf_counter
from typing import Any, NamedTuple

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

# NX-311 — CAUZA unui retry, vocabular ÎNCHIS. `llm_retry` singur a ascuns defectul o lună: numără
# la fel „furnizorul a picat" (repetarea are rost, cererea n-a ajuns) și „ne-am tăiat singuri"
# (repetarea pornește aceeași generare în fața aceluiași cronometru). Cer reparații OPUSE, deci nu
# pot împărți un contor. Ordinea de mai jos nu e cosmetică: în SDK `APITimeoutError` e SUBCLASĂ de
# `APIConnectionError`, deci un `isinstance` pus invers ar eticheta orice timeout drept „conexiune".
_RETRY_CAUSE_TIMEOUT = "llm_retry_timeout"
_RETRY_CAUSE_CONNECTION = "llm_retry_connection"
_RETRY_CAUSE_STATUS = "llm_retry_status"

# Apel care depășește VECHIUL perete de 30s. Pragul e înghețat deliberat la valoarea de dinainte de
# NX-311, nu legat de `llm_timeout_s`: rostul lui e să ridice CENZURA pe care ne-o puseserăm singuri
# (maximul observat al apelului de compunere era exact 30,1s, fiindcă acolo tăiam). Legat de config,
# seria ar deveni incomparabilă exact în momentul în care schimbi pragul, adică atunci când ai
# nevoie de ea.
_SLOW_CALL_AFTER_MS = 30_000
_SLOW_CALL_CODE = "llm_call_over_30s"


def _min_positive(*values: float | None) -> float | None:
    """Cel mai mic plafon ACTIV, sau `None` dacă niciunul nu e. Valorile ≤ 0 (buget deja consumat)
    devin un minim pozitiv, nu zero: un `timeout=0` înseamnă „fără timeout" pentru httpx, adică fix
    opusul intenției — o defecțiune care s-ar vedea abia ca un apel agățat la infinit."""
    active = [v for v in values if v is not None]
    return max(min(active), 0.001) if active else None


def _retry_cause(exc: Exception) -> str:
    """Excepția → codul de cauză. Timeout ÎNAINTEA conexiunii (vezi nota de mai sus)."""
    if isinstance(exc, openai.APITimeoutError | TimeoutError):
        return _RETRY_CAUSE_TIMEOUT
    if isinstance(exc, openai.APIConnectionError):
        return _RETRY_CAUSE_CONNECTION
    return _RETRY_CAUSE_STATUS


class Sampling(NamedTuple):
    """Ce pleacă pe sârmă + bitul pe baza căruia s-a decis. NX-311 îl scoate afară fiindcă `_chat`
    are nevoie de el pentru ceas, iar a-l recalcula separat ar însemna două derivări ale aceleiași
    întrebări, care pot diverge tăcut exact la schimbarea de model care a produs defectul."""

    params: dict[str, Any]
    reasoning_on: bool


class CallBudget(NamedTuple):
    """NX-311 — UN proprietar al întrebării „cât are voie apelul ăsta" (P3).

    Există fiindcă apelurile de model nu sunt de același fel, iar codul o știe deja: `_sampling`
    decide DETERMINIST dacă cererea raționează (pe bucla cu tool-uri raționamentul e FORȚAT `none`,
    altfel furnizorul dă 400). Același bit decide și ceasul — nu se inventează o taxonomie paralelă
    de „roluri", care ar putea diverge tăcut de cea care decide legalitatea cererii.

    `cap_ms` e plafonul NX-241 (activ doar cu deadline-ul de tur aprins), `attempt_timeout_s` e
    ceasul UNEI încercări, `total_cap_s` e plafonul întregii bucle de retry. Toate `None` = traseul
    de dinainte de card, byte-identic.
    """

    attempt_timeout_s: float | None
    total_cap_s: float | None
    cap_ms: int | None
    slow_after_ms: int | None


def call_budget(settings: Any, *, reasoning_on: bool) -> CallBudget:
    """Bugetul unui apel de chat. PUR (citește settings, nu atinge ceasul) → testabil direct."""
    legacy_cap = getattr(settings, "llm_call_cap_ms", 8_000)
    if not getattr(settings, "llm_call_budget_by_role_enabled", True):
        return CallBudget(None, None, legacy_cap, None)
    total = float(getattr(settings, "llm_call_total_cap_s", 90.0))
    if reasoning_on:
        return CallBudget(
            float(getattr(settings, "llm_timeout_reasoning_s", 75.0)),
            total,
            int(getattr(settings, "llm_call_cap_reasoning_ms", 60_000)),
            _SLOW_CALL_AFTER_MS,
        )
    return CallBudget(
        float(getattr(settings, "llm_timeout_s", 30.0)),
        total,
        legacy_cap,
        _SLOW_CALL_AFTER_MS,
    )


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
    factory: Callable[[float | None], Awaitable[Any]],
    *,
    max_retries: int,
    cap_ms: int | None = None,
    attempt_timeout_s: float | None = None,
    total_cap_s: float | None = None,
    slow_after_ms: int | None = None,
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

    NX-311 — al doilea plafon, care există ȘI cu deadline-ul de tur stins: `total_cap_s` mărginește
    ÎNTREAGA buclă, pe un ceas MONOTON local. Fără el, un timeout pe încercare dimensionat onest
    pentru un apel care raționează (75s) × 3 încercări ar însemna 225s — aș fi reparat p50 stricând
    coada. Cu el, plafonul dur pe un apel rămâne 90s, exact cât azi; se schimbă doar CE cumperi cu
    ei (o generare dusă până la capăt, nu trei tăiate la mijloc).

    `attempt_timeout_s` se pasează FACTORY-ului, nu se aplică aici cu `asyncio.timeout`: ceasul
    trebuie să fie al CERERII HTTP (SDK-ul închide conexiunea și ridică `APITimeoutError`), nu o
    anulare de corutină peste un request care rămâne în aer. `asyncio.timeout` de mai jos rămâne
    exclusiv al deadline-ului de TUR, ca `TimeoutError` să însemne în continuare un singur lucru.
    """
    d = deadline.current()
    min_useful = getattr(get_settings(), "llm_retry_min_budget_ms", 600)
    started = perf_counter()

    def _remaining_total_s() -> float | None:
        return None if total_cap_s is None else total_cap_s - (perf_counter() - started)

    delay = 0.5
    last: Exception | None = None
    for attempt in range(max_retries + 1):
        timeout_s = None if d is None else d.timeout_for(cap_ms)
        if timeout_s is not None and timeout_s <= 0:
            raise DeadlineExhausted("model", REASON_NO_ROOM)
        # Ceasul acestei încercări: capul ei, tăiat de cât a mai rămas din plafonul TOTAL. Cu
        # `total_cap_s=None` (kill-switch stins) rămâne exact `attempt_timeout_s`, adică `None`.
        call_timeout_s = _min_positive(attempt_timeout_s, _remaining_total_s())
        attempt_started = perf_counter()
        try:
            if timeout_s is None:
                return await factory(call_timeout_s)
            async with asyncio.timeout(timeout_s):
                return await factory(call_timeout_s)
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
        finally:
            # Se numără ȘI încercările REUȘITE, fiindcă exact ele sunt datele care lipseau: sub
            # vechiul perete un apel de 29s arăta la fel ca unul de 3s, iar unul de 40s nu exista.
            if slow_after_ms is not None:
                if (perf_counter() - attempt_started) * 1000.0 >= slow_after_ms:
                    turn_latency.degrade(_SLOW_CALL_CODE)
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
        # NX-311 — același raționament, pe plafonul TOTAL al apelului: dacă după somn n-ar mai
        # rămâne un minim util, încercarea următoare e bani și latență pe o generare pe care oricum
        # o tăiem la jumătate. Reutilizează codul de la NX-241, fiindcă e exact aceeași concluzie.
        remaining_total_s = _remaining_total_s()
        if remaining_total_s is not None and (remaining_total_s - sleep_s) * 1000.0 < min_useful:
            log.warning(
                "llm_api_failure: %s — retry abandonat, plafonul apelului e consumat (%.1fs/%.1fs)",
                type(last).__name__,
                (total_cap_s or 0.0) - remaining_total_s,
                total_cap_s or 0.0,
            )
            turn_latency.degrade("llm_retry_no_budget")
            break
        log.warning(
            "llm_api_failure: %s tranzitoriu (%s) — retry %d/%d în %.2fs",
            type(last).__name__,
            _retry_cause(last),
            attempt + 1,
            max_retries,
            sleep_s,
        )
        turn_latency.degrade("llm_retry")
        turn_latency.degrade(_retry_cause(last))
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


# ── Ce accepta o cerere NU e o preferinta a noastra, e o proprietate a MODULUI DE RATIONAMENT ──
# `temperature` si `reasoning_effort` sunt singurele optionale pe care le adaugam noi peste
# `chat.completions` (vezi `_sampling`). MASURAT pe API-ul real (2026-08-24, toate patru modelele
# din config + tool-urile REALE ale agentului), iar rezultatul nu se citeste pe familii de model, ci
# pe UN SINGUR bit — rationeaza cererea sau nu:
#
#   | rationament PORNIT  | `temperature` != 1 → 400 | function tools → 400 |
#   | rationament OPRIT   | `temperature` OK         | function tools OK    |
#
# „Pornit" inseamna `reasoning_effort` != `none` SAU (parametrul absent SI modelul rationeaza
# implicit). `gpt-5.6-*` rationeaza implicit; `gpt-5.4-*` nu. De aici tot ce parea contradictoriu:
#   • `gpt-5.4-mini` mergea nu fiindca ar fi „alta familie", ci fiindca implicit nu rationeaza;
#   • `gpt-5.6-luna` FARA niciun parametru pica pe tool-uri, fiindca implicitul lui e pornit;
#   • `gpt-5.4-mini` + `reasoning_effort=high` pica si el pe tool-uri — deci `high` singur ar fi
#     omorat calea de vanzare chiar daca modelul ramanea mini.
# Mesajul furnizorului o spune direct: „Function tools with reasoning_effort are not supported …
# To use function tools, use /v1/responses or set reasoning_effort to 'none'."
#
# CONSECINTA de produs, deliberata: cand cererea poarta tool-uri (adica bucla de vanzare), fortam
# `reasoning_effort='none'`. Pe `chat.completions` nu exista alta varianta, iar mutarea pe
# `/v1/responses` e o schimbare mare care se decide pe masuratori (D15), nu ca sa scapam de un 400.
# Deci `LLM_REASONING_EFFORT_AGENT` e INERT pe drumul cu tool-uri si activ pe apelurile de text/
# schema. Fortarea se NUMARA (`llm_reasoning_disabled_for_tools`), ca sa nu existe divergenta tacuta
# intre ce e configurat si ce pleaca.
#
# De ce e o poarta si nu o nota in docs: optionalele pleaca DOAR pe apelurile care trec prin
# `_chat`, iar `_with_retry` trateaza 4xx non-429 ca TERMINAL. O cerere prinsa pe picior gresit nu
# degradeaza, ci omoara exact calea pe care e trimisa — si numai pe aia, deci restul sistemului pare
# sanatos. S-a intamplat de doua ori: `max_tokens` pe `gpt-5.4-*` (PR #133) si combinatia adusa de
# `bbb77b3` pe 24 aug (default `gpt-5.4-mini` → `gpt-5.6-luna` PLUS `reasoning_effort=high`), cand
# fiecare tur de vanzare a cazut pe fallback-ul de runner cu triajul (nano) intact deasupra.
#
# Necunoscutul: prefix nedeclarat ⇒ niciun optional. Nu e „sigur" in absolut (un model care
# rationeaza implicit ar refuza tool-urile oricum, si n-avem ce trimite ca sa-l oprim), dar e cea
# mai mica presupunere pe care o putem face, iar smoke-ul de release o prinde la prima promovare.
#
# A TREIA optionala, masurata separat (2026-09-16, `scripts/preflight_structured_tools.py`):
# `response_format={"type": "json_schema", strict}` TRIMIS ODATA CU `tools`, pe `gpt-5.6-luna`,
# cu schema REALA a planului V2 si tool-urile reale ale agentului → ACCEPTAT (raspuns JSON valid).
# Conta fiindca `run_tool_loop_structured` (calea creierului unic, NX-239) le trimite impreuna pe
# FIECARE apel, in timp ce productia de azi le foloseste doar separat: `complete_schema` fara
# tool-uri (calea rich) si `run_tool_loop` fara schema (bucla de vanzare v1). Nu devine camp in
# `ModelProfile`: nimic nu l-ar impune, iar un camp neverificat sugereaza o garantie pe care n-o
# avem. Ramane masuratoare cu data pe ea, ca tabelul de mai sus, si se RE-RULEAZA la orice
# schimbare de `MODEL_AGENT` — un refuz aici nu degradeaza, omoara calea de vanzare intreaga.
_NO_REASONING = "none"


@dataclass(frozen=True)
class ModelProfile:
    """Ce stie adaptorul despre o familie de modele. `params` = optionalele care EXISTA pe ea;
    `reasons_by_default` = daca cererea rationeaza cand nu-i spui nimic."""

    params: frozenset[str]
    reasons_by_default: bool


# GPT-6 (2026-09-24, din documentatia OpenAI, NU masurat inca pe API): aceeasi regula a bitului.
# `luna` si `sol` accepta `none` si rationeaza implicit (`medium`), deci intra ca `gpt-5.6-`.
# `astra` NU accepta `none`: pe `chat.completions` n-ar putea rula bucla cu tool-uri deloc, deci
# ramane DELIBERAT nedeclarat (prefix necunoscut ⇒ numarat), nu prins de un prefix `gpt-6-` generic.
_MODEL_PROFILES: tuple[tuple[str, ModelProfile], ...] = (
    ("gpt-6-luna", ModelProfile(frozenset({"reasoning_effort", "temperature"}), True)),
    ("gpt-6-sol", ModelProfile(frozenset({"reasoning_effort", "temperature"}), True)),
    ("gpt-5.6-", ModelProfile(frozenset({"reasoning_effort", "temperature"}), True)),
    ("gpt-5.4-", ModelProfile(frozenset({"reasoning_effort", "temperature"}), False)),
)


def model_profile(model: str) -> ModelProfile | None:
    """Profilul familiei lui `model`, sau None daca prefixul nu e declarat."""
    for prefix, profile in _MODEL_PROFILES:
        if model.startswith(prefix):
            return profile
    return None


def supported_params(model: str) -> frozenset[str]:
    """Optionalele care EXISTA pe `model`. Prefix nedeclarat → niciunul.

    Atentie: „exista" nu e „e valid acum" — `temperature` exista pe ambele familii, dar e refuzata
    cand cererea rationeaza. Decizia efectiva e in `_sampling`, care stie si daca sunt tool-uri."""
    profile = model_profile(model)
    return profile.params if profile else frozenset()


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


#: Cheia de rutare a cache-ului de prompt pentru turul curent (NX-275 felia 3). ContextVar, ca
#: `usage` / `turn_latency` / `deadline`: valoarea aparține TURULUI, nu unei semnături de apel.
_prompt_cache_key: ContextVar[str] = ContextVar("prompt_cache_key", default="")


@contextmanager
def prompt_cache_scope(key: str) -> Iterator[None]:
    """Toate apelurile din bloc cer aceeași partiție de cache la furnizor.

    Fără cheie, ruterul OpenAI distribuie cererile după un hash implicit al prefixului; cu trafic
    mic și mai multe instanțe, două ture ale aceluiași tenant pot nimeri noduri diferite și niciunul
    nu găsește prefixul celuilalt. Cheia e `business_id` + versiunea de prompt: leagă turele care
    CHIAR au același prefix și le separă pe cele care n-au (o schimbare de prompt nu trebuie să
    caute într-un cache al versiunii vechi). NU conține `conversation_id`: ar face fiecare
    conversație propria partiție, adică exact opusul scopului.
    """
    token = _prompt_cache_key.set(key or "")
    try:
        yield
    finally:
        _prompt_cache_key.reset(token)


def _note_cache_on_span(resp: Any) -> None:
    """NX-275 felia 1: tokenii serviți din cache, pe spanul APELULUI curent.

    Aici, și nu în `usage.record_chat`, dintr-un motiv de poziție: `record_chat` e chemat DUPĂ ce
    `with turn_latency.span("model")` s-a închis, deci un atribut pus acolo n-ar avea pe ce să
    stea. `_chat` e wrapperul unic al tuturor apelurilor și rulează ÎNĂUNTRUL spanului.

    Ce se câștigă față de totalul pe tur: pe un tur cu două apeluri, primul SCRIE cache-ul și al
    doilea îl citește. Însumate, cele două arată o valoare de mijloc care nu spune dacă prefixul
    chiar se cache-uiește. Separate, apelul 2 răspunde direct. De cifra asta atârnă dacă schema de
    `response_format` costă 1x sau 0,1x (vezi `docs/NX-275-DESIGN.md` §0)."""
    try:
        cached = usage.cached_tokens_of(resp)
        if cached:
            from src.observability import tracing  # noqa: PLC0415 — evită ciclu la import

            tracing.set_attribute("tokens_cached", cached)
    except Exception:  # noqa: BLE001 — ca mai sus (P6, P10)
        return


def _note_call(shape: str, reasoning: bool, started: float, resp: Any, *, ok: bool) -> None:
    """NX-312: rândul apelului în `llm_usage.per_call`. Best-effort, ca `_note_cache_on_span`: o
    măsurătoare care aruncă ar transforma un apel reușit într-unul eșuat (P6)."""
    try:
        usage.record_call(
            resp, shape=shape, reasoning=reasoning, ms=(perf_counter() - started) * 1000.0, ok=ok
        )
    except Exception:  # noqa: BLE001
        return


class LLMClient:
    """Wrapper subțire peste AsyncOpenAI. Modelele vin din settings (nano/mini)."""

    def __init__(
        self,
        client: AsyncOpenAI,
        *,
        model_agent: str,
        model_embed: str = "text-embedding-3-small",
        model_moderation: str = "omni-moderation-latest",
        model_vision: str = "gpt-5.6-terra",
    ) -> None:
        self._client = client
        self.model_agent = model_agent
        self.model_embed = model_embed
        self.model_moderation = model_moderation
        self.model_vision = model_vision

    def _sampling(self, *, agent: bool, model: str, has_tools: bool = False) -> "Sampling":
        """Params trimiși la chat.completions. Ordinea contează: întâi decidem modul de
        RAȚIONAMENT, fiindcă de el atârnă și `temperature`, și dreptul de a trimite tool-uri
        (vezi tabelul de la `_MODEL_PROFILES`).

        • Plafon de output: `max_completion_tokens`, DOAR dacă `LLM_MAX_TOKENS_AGENT > 0`.
          Implicit e 0 = fără plafon, fiindcă tokenii de raționament ies din ACELAȘI buget ca
          textul: un cap dimensionat pentru text taie răspunsuri normale la mijloc fără să
          oprească vreo buclă (bucla e în RUNDE — vezi `run_tool_loop`). Motivul complet +
          măsurătoarea din producție: comentariul de la `llm_max_tokens_agent` în `config`.
          Când e trimis, folosim `max_completion_tokens`, NU `max_tokens` (deprecat → 400 pe
          modelele curente); e acceptat de toate familiile, deci nu trece prin poartă.
        • `reasoning_effort`: `llm_reasoning_effort_agent` pe apelurile de agent FĂRĂ tool-uri;
          FORȚAT `none` când cererea poartă tool-uri (altfel 400, indiferent de model).
        • `temperature`: gated de `llm_sampling_enabled` ȘI de modul de raționament. Cu
          raționamentul pornit, furnizorul acceptă doar valoarea implicită (1), deci nu trimitem
          nimic. Corectitudinea NU depinde de temperatură (o asigură validatorul stagiului 8), deci
          pierderea ei e o pierdere de varietate, nu de adevăr.

        Orice opțional lăsat acasă se NUMĂRĂ, ca să nu existe divergență tăcută între ce e
        configurat și ce pleacă pe sârmă.

        Triajul (agent=False) nu primește ceiling (JSON scurt). embed/moderate/vision nu trec
        pe aici."""
        s = get_settings()
        profile = model_profile(model)
        out: dict[str, Any] = {}
        if agent and s.llm_max_tokens_agent > 0:
            # 0 (implicit) = FĂRĂ plafon: parametrul nu se trimite. Nu se NUMĂRĂ — e starea
            # normală, iar un contor care se aprinde la fiecare apel e zgomot care se învață să
            # fie ignorat. Ce deployment rulează cu ce plafon se vede în `config_revision`
            # (`ops/build_info`), iar un plafon prea mic se vede în `llm_output_truncated_empty`.
            out["max_completion_tokens"] = s.llm_max_tokens_agent
        if profile is None:
            # Prefix nedeclarat: nu inventăm capabilități. Un 400 aici e zgomotos și reparabil
            # printr-o linie în `_MODEL_PROFILES`; un parametru ghicit e un tur pierdut tăcut.
            turn_latency.degrade("llm_model_profile_unknown")
            # NX-311: prefix nedeclarat ⇒ nu ȘTIM dacă raționează, iar bugetul cere un răspuns.
            # Alegem `False`, adică ceasul de azi (30s): un model necunoscut nu trebuie să capete
            # tăcut fereastra largă. Cazul e deja numărat mai sus, deci nu degradează pe tăcute.
            return Sampling(out, reasoning_on=False)

        wanted = (getattr(s, "llm_reasoning_effort_agent", "") or "").strip() if agent else ""
        effort = wanted
        if has_tools and "reasoning_effort" in profile.params:
            # Function tools nu sunt suportate pe `chat.completions` cu raționamentul pornit, iar
            # ABSENȚA parametrului nu ajunge: pe un model care raționează implicit, tot 400 iese.
            # Deci `none` se trimite EXPLICIT, nu se omite.
            effort = _NO_REASONING
            if wanted and wanted != _NO_REASONING:
                turn_latency.degrade("llm_reasoning_disabled_for_tools")
        if effort:
            if "reasoning_effort" in profile.params:
                out["reasoning_effort"] = effort
            else:
                turn_latency.degrade("llm_param_unsupported_reasoning_effort")

        # Raționează cererea? Explicit bate implicit; fără parametru, decide familia.
        reasoning_on = effort != _NO_REASONING if effort else profile.reasons_by_default
        if s.llm_sampling_enabled:
            if "temperature" in profile.params and not reasoning_on:
                out["temperature"] = (
                    s.llm_temperature_agent if agent else s.llm_temperature_background
                )
            else:
                turn_latency.degrade("llm_param_unsupported_temperature")
        return Sampling(out, reasoning_on=reasoning_on)

    async def _chat(self, *, agent: bool, **kwargs: Any):
        """Wrapper unic pe chat.completions.create: retry bounded (NX-126) + sampling params.

        NX-241: plafonul de timp al UNUI apel (`llm_call_cap_ms`) intră aici, nu în fiecare
        apelant — timeoutul efectiv rămâne `min(cap, buget rămas − rezervă)`.

        NX-311: tot aici intră și ceasul apelului, derivat din ACELAȘI bit care decide dacă cererea
        e legală (`Sampling.reasoning_on`). Clientul e un singleton per proces, deci nu poate purta
        două ceasuri — `timeout` pleacă per CERERE."""
        sampling = self._sampling(
            agent=agent, model=kwargs["model"], has_tools=bool(kwargs.get("tools"))
        )
        kwargs.update(sampling.params)
        s = get_settings()
        # NX-275 felia 3: sub același flag ca layoutul, fiindcă amândouă sunt inutile una fără
        # cealaltă (un prefix stabil pe care ruterul îl trimite în altă parte nu se cache-uiește,
        # și invers). Stins → parametrul nu pleacă deloc pe sârmă: OFF rămâne byte-identic.
        cache_key = _prompt_cache_key.get()
        if cache_key and getattr(s, "prompt_cache_layout_enabled", False):
            kwargs["prompt_cache_key"] = cache_key
        budget = call_budget(s, reasoning_on=sampling.reasoning_on)
        # NX-312: forma se citește ÎNAINTE de apel, din cererea care chiar pleacă pe sârmă.
        shape = usage.request_shape(kwargs)
        started = perf_counter()
        try:
            resp = await _with_retry(
                lambda t: self._client.chat.completions.create(
                    **(kwargs if t is None else {**kwargs, "timeout": t})
                ),
                max_retries=s.llm_retry_max,
                cap_ms=budget.cap_ms,
                attempt_timeout_s=budget.attempt_timeout_s,
                total_cap_s=budget.total_cap_s,
                slow_after_ms=budget.slow_after_ms,
            )
        except BaseException:
            # Și apelul EȘUAT e un rând: timpul lui a fost plătit de tur. Re-ridicăm neatins —
            # măsurătoarea nu are voie să schimbe ce vede apelantul (P6, P10).
            _note_call(shape, sampling.reasoning_on, started, None, ok=False)
            raise
        _note_call(shape, sampling.reasoning_on, started, resp, ok=True)
        _note_truncation(resp, cap=kwargs.get("max_completion_tokens"))
        _note_cache_on_span(resp)
        return resp

    async def classify_json(self, system: str, user: str, *, model: str | None = None) -> dict:
        """Apel chat cu răspuns JSON forțat (`response_format=json_object`).

        Întoarce dict-ul parsat. NX-297: singurul consumator rămas e extracția de fundal
        (profil + lead score, POST-tur) — triajul, pentru care fusese scris, nu mai există.
        `agent=False` nu mai înseamnă „nano", înseamnă „apel care nu e pe drumul răspunsului":
        fără plafonul de output al agentului, cu temperatura de fundal. Ridică la JSON invalid /
        eroare de API — caller-ul prinde și degradează."""
        mdl = model or self.model_agent
        # NX-300: un apel de model e o fază `model` oriunde ar rula. Aici rulează POST-tur, deci
        # acumulatorul activ e al aftercare-ului (`run_aftercare`), nu al turului — separarea vine
        # din CINE e împins în context, nu dintr-o etichetă de fază diferită. Exact ca `llm_usage`.
        with turn_latency.span("model"):
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
        # NX-300: apelul ăsta compune răspunsul bogat al căii v1 (`finalize._rich`) și era SINGURUL
        # apel de model de pe drumul sincron fără span. Pe turul `3e582c6d` a luat 86,7 s dintr-un
        # tur de 95,2 s, iar `turn_latency` raporta 6,2 s în faze — 91% din tur, invizibil.
        with turn_latency.span("model"):
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
        with turn_latency.span("model"):  # NX-300: niciun apel de `_chat` fără fază (test AST)
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
        stop_after_tools: Callable[[list[str]], bool] | None = None,
    ) -> str:
        """Buclă de tool-calling (agentul, G7). Modelul cere tool-uri → `execute(name, args)`
        le rulează (callback-ul agentului, întoarce `llm_view`) → rezultatele intră înapoi în
        conversație → repetă. CAP DUR `max_steps` (CLAUDE.md: max 3 tool calls/tur); la atingere
        forțează un text final FĂRĂ tools. Formatul OpenAI (tool_calls / rol `tool`) stă DOAR
        aici (adaptorul = singurul loc care vorbește OpenAI). Întoarce textul final.

        `execute` poate fi chemat de mai multe ori într-un pas (modelul cere ≥1 tool) — le
        rulăm CONCURENT (`asyncio.gather`) ca să tăiem latența.

        NX-312: `stop_after_tools(nume_unelte)` = predicatul AGENTULUI, chemat după ce o rundă
        și-a rulat uneltele. `True` ⇒ bucla iese cu `""`, fără runda de text care ar fi urmat:
        apelantul compune singur din ce au adus uneltele. Adaptorul nu știe de ce (P3); un
        predicat care aruncă e ignorat și bucla continuă ca înainte, fiindcă o rundă în plus e o
        pierdere de timp, iar un tur pierdut e o pierdere de client."""
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
            if stop_after_tools is not None:
                try:
                    stop = bool(stop_after_tools([tc.function.name for tc in tool_calls]))
                except Exception as e:  # noqa: BLE001 — predicatul nu are voie să piardă turul
                    log.warning("llm: stop_after_tools a eșuat (%s)", type(e).__name__)
                    stop = False
                if stop:
                    return ""

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
        seed: list[dict[str, Any]] | None = None,
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
        # NX-275 felia 6: retrieval speculativ. Perechea (assistant tool_call, tool result) se
        # inserează DUPĂ user, exact unde ar fi apărut dacă modelul ar fi cerut căutarea el însuși.
        # NU consumă o rundă: `rounds` numără apelurile de MODEL, iar aici n-a fost niciunul. Tool
        # call-ul e însă real în ledgerul NX-241, fiindcă a trecut prin `admit` la execuție.
        if seed:
            messages.extend(seed)
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
            # NX-311: `_t` ignorat DELIBERAT. Bugetul pe rol e al apelurilor de GENERARE, a căror
            # durată depinde de cât gândește modelul. Moderation/embed/vision sunt extracții cu
            # durată ~fixă, își au deja plafonul lor (`MODERATION_CAP_MS`, `embed_timeout_ms`), iar
            # `timeout` din constructor le rămâne anti-hang. Nu sunt membri ai clasei reparate aici.
            lambda _t: self._client.moderations.create(
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
            lambda _t: self._client.chat.completions.create(  # `_t`: vezi nota din `moderate`
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
            lambda _t: self._client.embeddings.create(  # `_t`: vezi nota din `moderate`
                model=mdl, input=texts
            ),
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
            model_agent=s.model_agent,
            model_embed=s.model_embed,
            model_moderation=s.model_moderation,
            model_vision=s.model_vision,
        )
    return _llm
