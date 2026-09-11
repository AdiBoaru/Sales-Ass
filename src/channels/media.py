"""Registry de `MediaFetcher` pentru worker — download de media inbound (Vision NX-76).

Singleton per proces (ca `get_llm`/`get_redis`): un `MediaFetcherRegistry` construit la primul
apel. Gate-ul (stagiul 3) cere fetcher-ul după `channel_kind` și aduce binarul unei poze înainte
de Vision — zero cod de canal în pipeline (cuplajul de transport stă la margini).

NX-289: registry-ul e GOL. Singurul fetcher implementat era `MetaClient.fetch_media` (WhatsApp),
iar `webchat` nu trimite media inbound — deci gate-ul degradează fail-soft pe `no_downloader`
(nicio poză rutată, dar nici excepție). Păstrăm seam-ul, nu un `if` de rescris: un canal care
aduce binar se înregistrează aici, iar restul pipeline-ului rămâne neatins.
Niciun secret/URL de media nu se loghează (P12, ca dispatcher-ul).
"""

from __future__ import annotations

from src.channels.base import MediaFetcherRegistry

_registry: MediaFetcherRegistry | None = None


def build_media_registry() -> MediaFetcherRegistry:
    """Construiește registry-ul de fetchers (ca `build_registry` din dispatcher).
    Azi nu are ce înregistra — vezi docstring-ul modulului."""
    return MediaFetcherRegistry()


def get_media_registry() -> MediaFetcherRegistry:
    """Singleton per proces. Fără canal cu download configurat → registry gol, fără client de
    rețea inutil."""
    global _registry
    if _registry is None:
        _registry = build_media_registry()
    return _registry


async def close_media() -> None:
    """Eliberează registry-ul (la oprirea procesului worker). Idempotent."""
    global _registry
    _registry = None
