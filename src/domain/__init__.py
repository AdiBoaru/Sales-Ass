"""DomainPack — config per-(business, vertical) din DB+seed (NX-114).

Vezi `pack.py` (contract), `loader.py` (încărcare + merge), `defaults/` (seed JSON per vertical).
"""

from src.domain.loader import load_domain_pack
from src.domain.pack import DomainPack

__all__ = ["DomainPack", "load_domain_pack"]
