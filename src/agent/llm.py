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
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from openai import AsyncOpenAI

from src.agent import usage
from src.config import get_settings

log = logging.getLogger(__name__)


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

    async def classify_json(self, system: str, user: str, *, model: str | None = None) -> dict:
        """Apel chat cu răspuns JSON forțat (`response_format=json_object`).

        Întoarce dict-ul parsat. Folosit de triaj (clasificare rută). Modelul
        implicit e cel de triaj (nano). Ridică la JSON invalid / eroare de API —
        caller-ul (stagiul) prinde și degradează."""
        mdl = model or self.model_triage
        resp = await self._client.chat.completions.create(
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
        resp = await self._client.chat.completions.create(
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
        resp = await self._client.chat.completions.create(
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
            resp = await self._client.chat.completions.create(
                model=mdl, messages=messages, tools=tools, tool_choice="auto"
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
        resp = await self._client.chat.completions.create(model=mdl, messages=messages)
        usage.record_chat(resp, mdl)
        return (resp.choices[0].message.content or "").strip()

    async def moderate(self, text: str, *, model: str | None = None) -> ModerationResult:
        """Clasifică un mesaj cu endpointul de moderation OpenAI (gratuit, NU generare —
        principiul 2, ca embed). Folosit de Gates (NX-15) ÎNAINTE de triaj. Ridică la
        eroare de API — caller-ul (gate) prinde și degradează fail-open."""
        resp = await self._client.moderations.create(
            model=model or self.model_moderation,
            input=text,
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
        resp = await self._client.chat.completions.create(
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
        )
        usage.record_chat(resp, mdl)
        return (resp.choices[0].message.content or "").strip()

    async def embed(self, texts: list[str], *, model: str | None = None) -> list[list[float]]:
        """Embeddings pentru un lot de texte. Întoarce o listă de vectori (1536 dim
        la text-embedding-3-small). Folosit de jobul `embed_products` + (viitor)
        cache semantic / search semantic."""
        mdl = model or self.model_embed
        resp = await self._client.embeddings.create(
            model=mdl,
            input=texts,
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
        _llm = LLMClient(
            AsyncOpenAI(api_key=s.openai_api_key),
            model_triage=s.model_triage,
            model_agent=s.model_agent,
            model_embed=s.model_embed,
            model_moderation=s.model_moderation,
            model_vision=s.model_vision,
        )
    return _llm
