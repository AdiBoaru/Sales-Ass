"""Adaptor OpenAI (async) — SINGURUL loc care vorbește cu API-ul OpenAI.

Folosit de stagiile LLM (triaj nano, agent mini) și de jobul de embeddings.
Clientul `AsyncOpenAI` e injectabil → testele pasează un fake (zero apeluri reale
în CI, ca testele integration). Fără cheie configurată → `get_llm()` întoarce
`None`, iar pipeline-ul degradează grațios (echo), nu crapă (principiul 6).

LLM se apelează DOAR din stagiile triaj și agent (principiul 2) — adică prin
acest adaptor, niciodată direct din alt cod.
"""

import json
import logging
from dataclasses import dataclass

from openai import AsyncOpenAI

from src.config import get_settings

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class ModerationResult:
    """Rezultatul moderation-ului (NX-15). `categories` = doar categoriile True
    (ex. ['harassment', 'hate']) — fără corpul mesajului (principiul 12)."""

    flagged: bool
    categories: list[str]


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
    ) -> None:
        self._client = client
        self.model_triage = model_triage
        self.model_agent = model_agent
        self.model_embed = model_embed
        self.model_moderation = model_moderation

    async def classify_json(self, system: str, user: str, *, model: str | None = None) -> dict:
        """Apel chat cu răspuns JSON forțat (`response_format=json_object`).

        Întoarce dict-ul parsat. Folosit de triaj (clasificare rută). Modelul
        implicit e cel de triaj (nano). Ridică la JSON invalid / eroare de API —
        caller-ul (stagiul) prinde și degradează."""
        resp = await self._client.chat.completions.create(
            model=model or self.model_triage,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            response_format={"type": "json_object"},
        )
        content = resp.choices[0].message.content or "{}"
        return json.loads(content)

    async def complete(self, system: str, user: str, *, model: str | None = None) -> str:
        """Apel chat care întoarce TEXT simplu (nu JSON). Modelul implicit = agent
        (mini). Folosit de agent pentru a compune recomandarea. Ridică la eroare de
        API — caller-ul prinde și degradează."""
        resp = await self._client.chat.completions.create(
            model=model or self.model_agent,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        )
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

    async def embed(self, texts: list[str], *, model: str | None = None) -> list[list[float]]:
        """Embeddings pentru un lot de texte. Întoarce o listă de vectori (1536 dim
        la text-embedding-3-small). Folosit de jobul `embed_products` + (viitor)
        cache semantic / search semantic."""
        resp = await self._client.embeddings.create(
            model=model or self.model_embed,
            input=texts,
        )
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
        )
    return _llm
