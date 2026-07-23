"""Sursa UNICĂ a versiunii de prompt folosite ca dimensiune de cheie în `semantic_cache` (NX-216).

De ce un modul separat, pentru o funcție de o linie: citirea (`worker/stages/cache.py`) și
scrierea (`worker/aftercare.py`) TREBUIE să folosească exact aceeași valoare. Când sursa era
implicită (default de parametru într-un loc, valoare hardcodată în altul), divergența a trecut
neobservată — vezi `tasks/NX-216.md`. Cu un singur apel, o schimbare de politică se face
într-un singur loc și nu poate desincroniza cele două capete.

Azi întoarce mereu `"v1"`. Când NX-181 (Prompt vNext) aterizează în `main`, AICI se leagă
flagul efectiv per business — o singură linie, fără să atingi cache_stage/aftercare:

    from src.agent.prompt_builder import prompt_vnext_effective
    return "vnext" if prompt_vnext_effective(business) else "v1"
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from src.models import BusinessConfig

# Namespace-ul implicit. Orice valoare nouă = rânduri DISTINCTE în semantic_cache (nu se
# suprascriu între ele) → schimbarea versiunii invalidează cache-ul prin construcție, nu prin purjă.
DEFAULT_PROMPT_VERSION = "v1"


def cache_prompt_version(business: BusinessConfig | None = None) -> str:  # noqa: ARG001
    """Versiunea de prompt sub care se citește ȘI se scrie cache-ul pentru acest business.

    `business` e primit deja acum (deși neutilizat) ca semnătura să nu se schimbe când NX-181
    leagă flagul per business — altfel ambele capete ar trebui atinse din nou, exact riscul
    pe care modulul îl elimină.
    """
    return DEFAULT_PROMPT_VERSION
