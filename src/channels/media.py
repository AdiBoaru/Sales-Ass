"""Registry de `MediaFetcher` pentru worker — download de media inbound (Vision NX-76, STT NX-75).

Singleton per proces (ca `get_llm`/`get_redis`): un `httpx.AsyncClient` + un `MediaFetcherRegistry`
construite la primul apel, închise la oprirea worker-ului. Gate-ul (stagiul 3) cere fetcher-ul după
`channel_kind` și aduce binarul unei poze/note vocale înainte de Vision/STT — zero cod de canal în
pipeline (cuplajul de transport stă la margini).

Azi DOAR WhatsApp suportă download (`MetaClient.fetch_media`); Telegram e canal de TEST (poller-ul
ignoră media). Fără token Meta → registry GOL → gate-ul degradează fail-soft (nicio poză rutată,
dar nici excepție). Niciun secret/URL de media nu se loghează (P12, ca dispatcher-ul).
"""

from __future__ import annotations

import httpx

from src.channels.base import MediaFetcherRegistry
from src.config import get_settings
from src.meta_client import MetaClient

_http: httpx.AsyncClient | None = None
_registry: MediaFetcherRegistry | None = None


def build_media_registry(http: httpx.AsyncClient | None, settings) -> MediaFetcherRegistry:
    """Construiește registry-ul de fetchers (ca `build_registry` din dispatcher). Înregistrează
    WhatsApp DOAR dacă există token + client http. Telegram = out of scope (media pe canal TEST)."""
    registry = MediaFetcherRegistry()
    if http is not None and settings.meta_access_token:
        registry.register("whatsapp", MetaClient(http, settings.meta_access_token))
    return registry


def get_media_registry() -> MediaFetcherRegistry:
    """Singleton per proces. Creează `httpx.AsyncClient` DOAR dacă e configurat un canal cu
    download (token Meta) — altfel întoarce un registry gol, fără client de rețea inutil."""
    global _http, _registry
    if _registry is None:
        s = get_settings()
        if s.meta_access_token:
            _http = httpx.AsyncClient(timeout=20.0)
        _registry = build_media_registry(_http, s)
    return _registry


async def close_media() -> None:
    """Închide clientul http (la oprirea procesului worker). Idempotent."""
    global _http, _registry
    if _http is not None:
        await _http.aclose()
        _http = None
    _registry = None
