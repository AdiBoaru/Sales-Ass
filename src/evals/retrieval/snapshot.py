"""NX-265 — amprenta CATALOGULUI sub un set de evaluare.

Manifestul setului (`tests/golden/retrieval_goldset/manifest.json`) apăra deja o singură direcție:
setul nu se poate edita fără să se vadă. Cealaltă direcție lipsea, și e cea care se întâmplă
singură — **catalogul se mișcă sub set**. Un produs judecat „corect" azi poate fi dezactivat mâine
de un import, iar raportul de poimâine ar arăta un top-3 mai slab și ar numi diferența „regresie de
relevanță". Sau, mai rău, ar arăta unul mai bun după ce un import a adăugat 300 de produse pe care
nimeni nu le-a judecat.

Nicio verificare nu poate opri asta, și nici nu trebuie: catalogul unui client TREBUIE să se
schimbe. Ce trebuie e ca raportul s-o SPUNĂ, ca diferența dintre două rulări să fie atribuibilă.
De-aia amprenta se scrie în manifest la adnotare și se recalculează la raportare, iar comparația e
o observație publicată, nu o poartă care pică.

Ce intră în amprentă și de ce doar atât:

* `products_active` — denominatorul. Se schimbă la fiecare import și explică singur mișcările mari;
* `max_synced_at` — prospețimea, citită din `synced_at`, NU din `updated_at`: al doilea se mișcă la
  orice atingere de rând (inclusiv la scrierile derivării NX-268), deci ar raporta drift acolo unde
  n-a intrat nicio dată nouă de la client.

**Versiunea de schemă lipsește deliberat, și merită spus de ce.** Ar fi fost al treilea câmp
evident: o migrare chiar schimbă ce se poate căuta (046 a rescris `search_tsv`, 047 a făcut stocul
UNKNOWN reprezentabil), deci două rulări peste o migrare nu compară același sistem. Măsurat pe baza
live, `bot_runtime` primește `InsufficientPrivilegeError` pe `schema_migrations` — tabela e de
MEDIU, nu de tenant. Ar fi mers pe `admin_conn`, dar CLAUDE.md declară exact două excepții de
control plane și spune că orice alt query pe el e un bug de izolare; un harness de evaluare nu e a
treia. Deci câmpul ar fi ieșit `null` la fiecare rulare, în amândouă, adică un semnal care arată ca
o măsurătoare și nu poate detecta nimic — exact clasa de tăcere pe care Wave H o vânează. Migrarea
se vede oricum în `git log` al raportului comis.

NU intră nici un hash peste tot catalogul. Ar fi mai strict și mai inutil: pe 2.758 de produse orice
atingere l-ar schimba, deci ar fi permanent „driftat" și nimeni nu s-ar mai uita la el. O amprentă
care e mereu roșie e la fel de mută ca una absentă.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

__all__ = ["CatalogSnapshot", "compare", "read_snapshot"]


@dataclass(frozen=True, slots=True)
class CatalogSnapshot:
    """Starea catalogului la un moment dat. Serializabilă, comparabilă, fără PII."""

    products_active: int
    max_synced_at: str | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "products_active": self.products_active,
            "max_synced_at": self.max_synced_at,
        }

    @classmethod
    def from_dict(cls, raw: Any) -> CatalogSnapshot | None:
        """`None` pe orice formă neașteptată — un manifest vechi (fără amprentă) nu e o eroare, e
        un set judecat înainte ca amprenta să existe. Raportul spune „necunoscut", nu „driftat":
        a numi absența unei măsurători „schimbare" e chiar felul de minciună pe care modulul ăsta
        încearcă s-o prevină."""
        if not isinstance(raw, dict):
            return None
        try:
            return cls(
                products_active=int(raw["products_active"]),
                max_synced_at=(
                    str(raw["max_synced_at"]) if raw.get("max_synced_at") is not None else None
                ),
            )
        except (KeyError, TypeError, ValueError):
            return None


_SQL = """
select (select count(*) from products
         where business_id = $1 and status = 'active')                    as products_active,
       (select max(synced_at)::text from products where business_id = $1) as max_synced_at
"""


async def read_snapshot(conn: Any, business_id: str) -> CatalogSnapshot:
    """Amprenta curentă. Read-only, tenant-scoped (P7)."""
    row = await conn.fetchrow(_SQL, business_id)
    return CatalogSnapshot(
        products_active=int(row["products_active"] or 0),
        max_synced_at=row["max_synced_at"],
    )


def compare(sealed: CatalogSnapshot | None, current: CatalogSnapshot) -> dict[str, Any]:
    """Ce s-a mișcat între sigilarea setului și rularea de acum.

    Verdictul are TREI valori, nu două, din același motiv ca la NX-238 și NX-246 felia 3:
    `unknown` („setul e mai vechi decât amprenta") nu e `same`. Un raport care confundă „n-am cu ce
    compara" cu „nu s-a schimbat nimic" dă exact falsa liniște pe care instrumentul o combate.
    """
    if sealed is None:
        return {
            "verdict": "unknown",
            "note": "setul a fost sigilat înainte ca amprenta de catalog să existe",
        }
    changes = {
        field: {"sealed": s, "current": c}
        for field, s, c in (
            ("products_active", sealed.products_active, current.products_active),
            ("max_synced_at", sealed.max_synced_at, current.max_synced_at),
        )
        if s != c
    }
    if not changes:
        return {"verdict": "same"}
    return {"verdict": "drifted", "changed": changes}
