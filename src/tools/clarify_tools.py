"""`clarify_options` — ce poate ONORA magazinul, pentru întrebarea pe care agentul urmează s-o pună.

## De ce o unealtă și nu un paragraf în promptul de system

Promptul de system e plătit pe fiecare tur, iar clarificările sunt o minoritate. Mai important:
opțiunile nu sunt cunoaștere GENERALĂ, sunt starea catalogului ACESTUI tenant, pe raftul discutat
ACUM. Un paragraf în prompt n-ar putea decât să-i spună modelului „întreabă despre ceva ce avem",
adică exact lucrul pe care nu-l poate verifica singur — și de aici a venit defectul măsurat la
NX-295: un magazin de cosmetice care oferea «Pentru consola, cablu USB de date».

Deci: `description` spune CÂND se cheamă (se plătește mereu, e cache-uit, e scurt), iar `llm_view`
spune CUM se folosește ce a venit (se plătește doar când unealta chiar e chemată). Regulile de
formulare călătoresc cu datele lor, nu separat de ele.

## Ce întoarce

Meniul ÎNCHIS al catalogului (`src/catalog/clarify_menu.py`, NX-295): rafturile servabile plus
fațetele care există CHIAR sub raftul discutat, fiecare frază trecută deja prin round-trip — se
rezolvă înapoi, prin ACEEAȘI `resolve_any` pe care o folosește căutarea, pe o cheie cu produse.
Fraza pe care agentul o citește aici e, prin construcție, o promisiune pe care magazinul o poate
onora.

**Fără cifre.** Meniul le are (numărul de produse pe cheie) și le folosește ca să ORDONEZE
opțiunile, dar nu i le dăm modelului: o cifră ajunsă în prompt e o cifră pe care o poate rosti, iar
validatorul stagiului 8 verifică prețuri și produse, nu inventarul pe raft.

**`catalog_miss` e o informație, nu o eroare.** Când codul a confruntat deja fiecare cuvânt al
cererii cu vocabularul tenantului și n-a găsit nimic, răspunsul onest e „nu vindem așa ceva", iar
instrucțiunea explicită e să NU ceară detalii despre produsul inexistent (culoare, mărime, model) —
ar fi o promisiune pe care magazinul n-o poate onora.

**Fail-OPEN, ca la NX-295.** Meniu indisponibil (flag stins, DB jos, vocabular gol) ⇒ `ok=False` cu
instrucțiunea de a întreba natural, fără să numească tipuri de produse. O clipeală de DB nu are
voie să devină „botul nu mai știe să întrebe".
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from src.catalog.clarify_menu import CATEGORY_DIMENSION, menu_for_turn
from src.tools.base import ToolResult, register

if TYPE_CHECKING:
    from src.catalog.clarify_menu import ClarifyMenu
    from src.models import TurnContext
    from src.worker.runner import PipelineDeps

#: Câte opțiuni intră în `llm_view`. Meniul e deja plafonat (`_MAX_OPTIONS`), dar plafonul e al LUI
#: și poate crește; ăsta e al PROMPTULUI. Două plafoane peste aceeași listă nu pot rămâne de acord
#: decât dacă al doilea e declarat aici, lângă textul pe care îl mărginește.
MAX_OFFERED = 10

#: Regulile de FORMULARE, lipite de datele lor. Nu intră în promptul de system (vezi docstring).
_RULES = (
    "Pune O SINGURĂ întrebare, scurtă, în limba clientului, ca un consultant care chiar vrea "
    "să nimerească. Poți numi opțiunile de mai sus, dar EXACT cu cuvintele de acolo: sunt "
    "singurele pe care magazinul le poate onora. Nu inventa altele și nu enumera tot raftul. "
    "Dacă din conversație știi deja destul cât să recomanzi, NU întreba: caută și arată produse."
)

_MISS_RULES = (
    "Cererea clientului NU se regăsește în catalogul magazinului: codul a confruntat deja "
    "fiecare cuvânt al ei cu vocabularul tenantului. Spune-i ONEST, într-o frază scurtă, că nu "
    "vindem așa ceva, și întreabă-l dacă îl putem ajuta cu ce avem. NU cere detalii despre "
    "produsul pe care nu-l avem (culoare, mărime, model): ar fi o promisiune neonorabilă."
)

_UNAVAILABLE = (
    "Nu am opțiuni de oferit pentru turul ăsta. Întreabă natural ce caută clientul, FĂRĂ să "
    "numești tipuri de produse sau categorii — n-am cu ce le garanta."
)


def _render(menu: ClarifyMenu) -> str:
    """Opțiunile grupate pe axă, în ordinea meniului. Fără cifre (vezi docstring-ul modulului)."""
    shelves = [o.phrase for o in menu.options if o.dimension == CATEGORY_DIMENSION][:MAX_OFFERED]
    facets = [o.phrase for o in menu.options if o.dimension != CATEGORY_DIMENSION][:MAX_OFFERED]
    lines: list[str] = []
    if shelves:
        lines.append("Rafturi pe care le avem: " + ", ".join(shelves))
    if facets:
        lines.append("Caracteristici care există sub ce discutați: " + ", ".join(facets))
    return "\n".join(lines)


@register("clarify_options")
async def clarify_options_tool(
    ctx: TurnContext, deps: PipelineDeps, args: dict[str, Any]
) -> ToolResult:
    """Opțiunile oferibile ale turului. Fără argumente: meniul se compune din mesajul clientului,
    din raftul discutat și din ce știe deja conversația, nu din ce declară modelul (P7)."""
    menu = await menu_for_turn(ctx, deps)
    ctx.emit(
        "clarify_options",
        n=len(menu.options),
        source=menu.reason,
        catalog_miss=menu.catalog_miss,
    )
    if menu.catalog_miss:
        # Ordinea contează: „nu vindem asta" bate orice listă de opțiuni. Meniul poate fi ne-gol
        # (rafturile există întotdeauna), iar oferit aici ar transforma un refuz onest într-o
        # întrebare despre un produs pe care nu-l avem.
        body = _render(menu)
        return ToolResult(
            ok=True,
            products=[],
            llm_view=f"{_MISS_RULES}\n{body}" if body else _MISS_RULES,
        )
    if not menu.usable:
        return ToolResult(ok=False, products=[], error="no_options", llm_view=_UNAVAILABLE)
    return ToolResult(ok=True, products=[], llm_view=f"{_render(menu)}\n{_RULES}")
