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

from src.agent import usage
from src.config import get_settings

log = logging.getLogger(__name__)

# Erori TRANZITORII fără status HTTP (timeout / conexiune) — retry-abile.
_TRANSIENT_ERRORS = (openai.APITimeoutError, openai.APIConnectionError)


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


async def _with_retry(factory: Callable[[], Awaitable[Any]], *, max_retries: int) -> Any:
    """NX-126: retry bounded pe erori TRANZITORII (429 / 5xx / timeout / connection). Respectă
    `Retry-After` când există, altfel backoff exponențial cu jitter. 4xx terminale (400/401/403/404)
    ridică imediat (caller-ul degradează — P6). La epuizare loghează `llm_api_failure` și ridică.
    Trăiește DOAR în adaptor (cuplajul OpenAI stă la margine)."""
    delay = 0.5
    last: Exception | None = None
    for attempt in range(max_retries + 1):
        try:
            return await factory()
        except openai.APIStatusError as e:
            # 429 (RateLimitError) + 5xx = tranzitoriu; restul 4xx = terminal → ridică.
            if e.status_code < 500 and not isinstance(e, openai.RateLimitError):
                raise
            last, wait = e, _retry_after_seconds(e)
        except _TRANSIENT_ERRORS as e:
            last, wait = e, None
        if attempt >= max_retries:
            break
        sleep_s = (wait if wait is not None else delay) + random.uniform(0.0, 0.25)
        log.warning(
            "llm_api_failure: %s tranzitoriu — retry %d/%d în %.2fs",
            type(last).__name__,
            attempt + 1,
            max_retries,
            sleep_s,
        )
        await asyncio.sleep(sleep_s)
        delay *= 2
    log.warning(
        "llm_api_failure: %s — epuizat după %d reîncercări", type(last).__name__, max_retries
    )
    raise last


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
        model_vision: str = "gpt-5.4-mini",
    ) -> None:
        self._client = client
        self.model_triage = model_triage
        self.model_agent = model_agent
        self.model_embed = model_embed
        self.model_moderation = model_moderation
        self.model_vision = model_vision

    def _sampling(self, *, agent: bool) -> dict[str, Any]:
        """NX-126: params de sampling din settings, gated de kill-switch (modele „reasoning" care
        resping `temperature` ne-default → LLM_SAMPLING_ENABLED=false). `max_tokens` DOAR pe
        apelurile de agent (triajul JSON e scurt; embed/moderate/vision nu trec pe aici)."""
        s = get_settings()
        if not s.llm_sampling_enabled:
            return {}
        out: dict[str, Any] = {"temperature": s.llm_temperature}
        if agent:
            out["max_tokens"] = s.llm_max_tokens_agent
        return out

    async def _chat(self, *, agent: bool, **kwargs: Any):
        """Wrapper unic pe chat.completions.create: retry bounded (NX-126) + sampling params."""
        kwargs.update(self._sampling(agent=agent))
        return await _with_retry(
            lambda: self._client.chat.completions.create(**kwargs),
            max_retries=get_settings().llm_retry_max,
        )

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
            resp = await self._chat(
                agent=True, model=mdl, messages=messages, tools=tools, tool_choice="auto"
            )
            usage.record_chat(resp, mdl)
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
            contents = await asyncio.gather(
                *(
                    execute(tc.function.name, _parse_args(tc.function.arguments))
                    for tc in tool_calls
                )
            )
            for tc, content in zip(tool_calls, contents, strict=True):
                messages.append({"role": "tool", "tool_call_id": tc.id, "content": content})

        # cap atins → un ultim apel FĂRĂ tools (text forțat, nu o a 4-a rundă de tool calls).
        resp = await self._chat(agent=True, model=mdl, messages=messages)
        usage.record_chat(resp, mdl)
        return (resp.choices[0].message.content or "").strip()

    async def moderate(self, text: str, *, model: str | None = None) -> ModerationResult:
        """Clasifică un mesaj cu endpointul de moderation OpenAI (gratuit, NU generare —
        principiul 2, ca embed). Folosit de Gates (NX-15) ÎNAINTE de triaj. Ridică la
        eroare de API — caller-ul (gate) prinde și degradează fail-open."""
        resp = await _with_retry(
            lambda: self._client.moderations.create(
                model=model or self.model_moderation, input=text
            ),
            max_retries=get_settings().llm_retry_max,
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
                max_tokens=120,
            ),
            max_retries=get_settings().llm_retry_max,
        )
        usage.record_chat(resp, mdl)
        return (resp.choices[0].message.content or "").strip()

    async def embed(self, texts: list[str], *, model: str | None = None) -> list[list[float]]:
        """Embeddings pentru un lot de texte. Întoarce o listă de vectori (1536 dim
        la text-embedding-3-small). Folosit de jobul `embed_products` + (viitor)
        cache semantic / search semantic."""
        mdl = model or self.model_embed
        resp = await _with_retry(
            lambda: self._client.embeddings.create(model=mdl, input=texts),
            max_retries=get_settings().llm_retry_max,
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
