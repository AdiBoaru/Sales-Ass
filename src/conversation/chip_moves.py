"""Un chip nu e un TEXT pe care îl validăm, e o MUTARE pe care serverul o poate executa.

## Problema

NX-295 a închis o gaură reală (magazin de cosmetice care oferea «Pentru consola, cablu USB de
date») punând un MENIU ÎNCHIS înaintea modelului. Prețul plătit se vede pe traficul de azi: pe
calea creierului unic, `brain._clarify_chips` cheamă `ground_suggestions` cu lista GOALĂ, deci
chips-urile SUNT chiar etichetele meniului — «Ten», «ten uscat», «crema de fata». Adevărate,
apăsabile, și complet mute: nu spun clientului unde îl duc. Bogăția de tip iZi («Arată-mi variante
mai simple, nu seturi mari») exista pe v1, unde textul era scris liber de modelul rich — și e
exact producătorul pe care `CHIP_PRODUCERS` îl declară NEANCORAT.

Deci avem două jumătăți care nu se ating: una adevărată și seacă, alta bogată și negarantată.

## Reparația

Se inversează ordinea. Azi: *modelul scrie o frază → încercăm s-o verificăm*. Aici:

    MUTARE (kind + sloturi + DOVADĂ calculată în turul ăsta)
      → prompt: mutările oferite, fiecare cu fraza-ancoră
        → plan: `chip_labels[{move_id, text}]`  (ACELAȘI apel, zero rundă în plus)
          → poartă PER MUTARE: textul trebuie să conțină ancora MUTĂRII LUI
            → cade poarta? se emite ȘABLONUL serverului, nu se pierde chip-ul

Poarta e per mutare, nu contra întregului vocabular, și diferența e chiar obiecția pentru care
NX-295 spune că `ground_suggestions` NU e poarta potrivită pentru follow-up-uri: acolo mulțimea de
comparat e tot vocabularul tenantului, iar un vocabular bogat are un cuvânt pentru aproape orice.
Aici mulțimea are UN element, deci nu mai poate trece din întâmplare.

**De ce poarta nu e cosmetică.** Pe contractul v1, apăsarea unui chip retrimite `label` ca MESAJ
NOU al clientului (`models.MAX_CHIP_LEN`). Dacă modelul reformulează într-o frază din care ancora
a dispărut, chip-ul arată bine și la apăsare duce în gol. Cerând ancora întreagă, garantăm că
fraza se rezolvă prin ACEEAȘI `resolve_any` pe care o folosește căutarea. Textul ESTE comanda.

**Asimetria, deliberată:** fail-CLOSED pe textul modelului (reformulare stricată ⇒ o aruncăm),
fail-OPEN pe mutare (modelul tace ⇒ rămâne șablonul). Nu putem pierde un chip fiindcă modelul a
fost leneș, și nu putem emite unul pe care l-a inventat.

**De ce nu se trunchiază niciodată un text verificat.** `MAX_CHIP_LEN` taie la 56 de caractere, iar
o tăiere APLICATĂ DUPĂ verificare ar putea scoate tocmai ancora — adică poarta ar spune „da" despre
un text, și la client ar pleca altul. Un label prea lung se RESPINGE (cade pe șablon, care e
construit cu `fit_template`, deci încape prin construcție).

## Ce e aici și ce nu

Sursa dovezii e, pentru fiecare mutare, date pe care turul le are DEJA:

  refine_facet / pivot_shelf  ← meniul închis (NX-295), deci round-trip-ul e deja făcut
  price_band                  ← prețurile cardurilor turului (prag care chiar DESPARTE setul)
  detail / reviews / compare  ← produsele tocmai afișate
  link                        ← produsul afișat care are url

Nu există aici o mutare „alternative din graful de relații", deși planul inițial o avea: ar cere
o interogare în plus pe drumul SINCRON, iar rolul ei (lateral) e deja acoperit de `pivot_shelf`,
care se compune din meniu fără niciun I/O nou. Se poate adăuga când există o măsurătoare care
arată că lipsește ceva, nu înainte (D15).

**Copy-ul nu e în cod.** Șabloanele trăiesc în `DomainPack.chip_templates` (per locale), fiindcă
limba e configurație, nu constantă (P11). Pachet fără șablon pentru o mutare ⇒ mutarea nu se
oferă. Alternativa — o frază românească scrisă aici ca fallback — ar fi exact ce interzice P11.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from src.agent.fallbacks import fit_template
from src.catalog.clarify_menu import contains_run, words_of
from src.catalog.render_text import display_name
from src.catalog.vocabulary import CATEGORY_DIMENSION
from src.models import MAX_CHIP_LEN

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping, Sequence

    from src.catalog.clarify_menu import ClarifyMenu

__all__ = [
    "MOVE_ROLES",
    "ChipMove",
    "apply_labels",
    "from_cards",
    "from_menu",
    "offer_block",
    "renderable",
    "render_move",
    "roles_for",
    "select",
]

ROLE_FORWARD = "forward"  # îngustează: clientul ajunge mai aproape de ce vrea
ROLE_LATERAL = "lateral"  # schimbă direcția: alt raft, altă familie de produs
ROLE_DEEPEN = "deepen"  # rămâne pe ce s-a arătat: detalii, recenzii, comparație
ROLE_COMMIT = "commit"  # pasul spre cumpărare

#: Vocabular ÎNCHIS de mutări. O mutare nedeclarată aici nu poate deveni chip (`render_move`
#: întoarce None), exact ca la `ActionSpec` (NX-236): registrul e poarta, nu o convenție.
MOVE_ROLES: dict[str, str] = {
    "refine_facet": ROLE_FORWARD,
    "price_band": ROLE_FORWARD,
    "pivot_shelf": ROLE_LATERAL,
    "detail": ROLE_DEEPEN,
    "reviews": ROLE_DEEPEN,
    "compare": ROLE_DEEPEN,
    "link": ROLE_COMMIT,
}

#: Câte mutări din ACELAȘI fel intră în selecție. Fără plafon, un meniu bogat în fațete umple
#: toate sloturile cu îngustări, iar clientul primește cinci feluri de a filtra și niciun fel de
#: a continua. Screenshotul iZi de la care a pornit cererea are exact forma asta: patru îngustări
#: și o cale laterală.
_MAX_PER_KIND = 2

#: Câte prețuri se uită la construirea benzii de preț. Cardurile turului, nu catalogul.
_MIN_PRICES_FOR_BAND = 3

#: Pragurile de rotunjire ale benzii de preț, de la mare la mic. Se rotunjește în JOS ca promisiunea
#: să rămână adevărată chiar dacă setul se schimbă puțin între ture.
_BAND_STEPS: tuple[int, ...] = (500, 100, 50, 25, 10)


@dataclass(frozen=True, slots=True)
class ChipMove:
    """O continuare pe care serverul o poate ONORA, nu o frază pe care o speră.

    `anchor` e partea din text fără de care mutarea nu se mai poate executa la apăsare: o frază
    de meniu (deci rezolvabilă prin `resolve_any`) sau numele scurt al unui produs afișat.
    `evidence` e numărul de produse din setul-țintă — o mutare cu `evidence <= 0` nu se oferă,
    fiindcă ar fi exact promisiunea neonorabilă pe care NX-295 a măsurat-o.
    """

    kind: str
    move_id: str
    anchor: str
    slots: tuple[tuple[str, str], ...]
    evidence: int

    @property
    def role(self) -> str:
        return MOVE_ROLES.get(self.kind, ROLE_FORWARD)

    @property
    def rephrasable(self) -> bool:
        """Poate fi oferită modelului spre reformulare?

        Doar mutările știute ÎNAINTE de apel (cele din meniu). Cele derivate din cardurile
        turului se nasc după ce planul e gata, deci n-au cum să ajungă în promptul aceluiași apel,
        iar un al doilea apel ca să le îmbrace ar fi relay-ul interzis de D1."""
        return self.kind in ("refine_facet", "pivot_shelf")

    def slot_map(self) -> dict[str, str]:
        return dict(self.slots)


def _templates(pack: object) -> Mapping[str, Mapping[str, str]]:
    value = getattr(pack, "chip_templates", None)
    return value if isinstance(value, dict) else {}


def _template(pack: object, kind: str, locale: str) -> str | None:
    """Șablonul mutării în limba clientului. Lipsă ⇒ None ⇒ mutarea nu se oferă (P11)."""
    per_kind = _templates(pack).get(kind) or {}
    if not isinstance(per_kind, dict):
        return None
    lang = (locale or "").strip().lower()
    value = per_kind.get(lang) or per_kind.get(lang.split("-")[0])
    return value if isinstance(value, str) and value.strip() else None


def render_move(move: ChipMove, pack: object, locale: str) -> str | None:
    """Textul SERVERULUI pentru o mutare. `None` = mutarea nu se poate oferi onest.

    Două motive, și al doilea a fost găsit rulând felia pe date reale, nu presupus. Primul:
    pachetul n-are șablon pentru mutare (P11). Al doilea: șablonul a intrat în plafon doar
    SCURTÂND ancora — «Compara Serum Hidratant LumaDe… cu Crema Bogata NordSkin». Textul arată
    plauzibil, dar numele trunchiat e exact partea pe care apăsarea trebuie s-o rezolve.

    Regula e ACEEAȘI pe care o aplicăm modelului în `apply_labels`: un text care nu conține ancora
    întreagă nu se emite. Ar fi fost incoerent să respingem reformularea modelului pentru asta și
    să lăsăm șablonul nostru să facă același lucru în tăcere. O comparație între două nume lungi
    pur și simplu nu încape în 56 de caractere, deci nu se oferă.
    """
    template = _template(pack, move.kind, locale)
    if template is None:
        return None
    try:
        text = fit_template(template, **move.slot_map())
    except (KeyError, IndexError):
        # Șablon cu alt slot decât produce mutarea: e o eroare de configurare a tenantului, nu un
        # motiv să crape turul. Mutarea pur și simplu nu se oferă.
        return None
    if move.anchor and not contains_run(words_of(text), words_of(move.anchor)):
        return None
    return text


def renderable(moves: Iterable[ChipMove], pack: object, locale: str) -> list[ChipMove]:
    """Doar mutările pe care le putem EXPRIMA (șablon prezent, ancoră întreagă în text).

    Se aplică ÎNAINTE de selecție, nu după, iar diferența e un slot pierdut: filtrată la randare,
    o mutare aleasă și apoi aruncată lăsa patru chips acolo unde catalogul avea cinci de oferit.
    Măsurat pe ieșirea reală a feliei — o comparație între două nume lungi nu încape în plafon,
    deci nu are ce căuta nici în prompt, nici în selecție.
    """
    return [m for m in moves if render_move(m, pack, locale) is not None]


# --- construcția mutărilor: fiecare familie din datele pe care turul le are deja ---------------


def from_menu(menu: ClarifyMenu, *, offered_before: Iterable[str] = ()) -> list[ChipMove]:
    """Mutările știute ÎNAINTE de apel: îngustări pe fațete + schimbări de raft.

    Round-trip-ul e deja făcut de `build_menu` (fiecare frază se rezolvă înapoi pe o cheie cu
    produse), deci aici nu se re-verifică nimic — s-ar verifica de două ori același lucru, cu două
    definiții care pot diverge. `count` devine `evidence`.
    """
    seen = {str(m) for m in offered_before}
    out: list[ChipMove] = []
    for option in menu.options:
        kind = "pivot_shelf" if option.dimension == CATEGORY_DIMENSION else "refine_facet"
        move_id = f"{kind}:{option.dimension}:{option.key}"
        if move_id in seen or option.count <= 0:
            continue
        out.append(
            ChipMove(
                kind=kind,
                move_id=move_id,
                anchor=option.phrase,
                slots=(("slot", option.phrase),),
                evidence=option.count,
            )
        )
    return out


def _price_band(prices: Sequence[float]) -> int | None:
    """Un prag care chiar DESPARTE setul afișat, rotunjit în jos la o cifră rostibilă.

    Regula de acceptare nu e „arată bine", e structurală: trebuie să rămână cel puțin un produs sub
    prag ȘI cel puțin unul peste. Un prag sub care intră tot nu îngustează nimic, iar unul sub care
    nu intră nimic e promisiunea goală pe care o evită tot modulul.
    """
    clean = sorted(p for p in prices if isinstance(p, (int, float)) and p > 0)
    if len(clean) < _MIN_PRICES_FOR_BAND:
        return None
    middle = clean[len(clean) // 2]
    for step in _BAND_STEPS:
        threshold = int(middle // step) * step
        if threshold <= 0:
            continue
        below = sum(1 for p in clean if p < threshold)
        if below and below < len(clean):
            return threshold
    return None


def from_cards(
    cards: Sequence[Mapping[str, Any]], *, offered_before: Iterable[str] = ()
) -> list[ChipMove]:
    """Mutările născute din ce tocmai s-a ARĂTAT: detaliu, recenzii, comparație, link, preț.

    Ancora e numele SCURT al produsului (`display_name`, aceeași regulă ca peste tot), fiindcă
    numele întreg din catalogul real are ~190 de caractere și n-ar încăpea într-un chip. Un card
    fără id sau fără nume nu produce mutări: un chip care numește un produs pe care nu-l putem
    rezolva la apăsare e fix defectul pe care îl evităm.
    """
    seen = {str(m) for m in offered_before}
    named: list[tuple[str, str]] = []
    prices: list[float] = []
    for card in cards:
        pid = card.get("product_id") or card.get("id")
        name = card.get("name")
        if not pid or not name:
            continue
        named.append((str(pid), display_name(str(name))))
        price = card.get("price")
        if isinstance(price, (int, float)):
            prices.append(float(price))

    out: list[ChipMove] = []

    def _add(kind: str, key: str, anchor: str, slots: dict[str, str], evidence: int) -> None:
        move_id = f"{kind}:{key}"
        if move_id in seen or evidence <= 0:
            return
        out.append(
            ChipMove(
                kind=kind,
                move_id=move_id,
                anchor=anchor,
                slots=tuple(sorted(slots.items())),
                evidence=evidence,
            )
        )

    for pid, short in named:
        _add("detail", pid, short, {"slot": short}, 1)
        _add("reviews", pid, short, {"slot": short}, 1)
        _add("link", pid, short, {"slot": short}, 1)
    if len(named) >= 2:
        (id_a, a), (id_b, b) = named[0], named[1]
        # Ancora comparației e PRIMUL nume: `fit_template` poate scurta al doilea slot ca să încapă,
        # iar o ancoră pe un text scurtat n-ar mai fi verificabilă.
        _add("compare", f"{id_a}:{id_b}", a, {"slot": a, "slot_b": b}, 2)
    threshold = _price_band(prices)
    if threshold is not None:
        _add(
            "price_band",
            str(threshold),
            str(threshold),
            {"slot": str(threshold)},
            sum(1 for p in prices if p < threshold),
        )
    return out


# --- selecția: ce iese la client, în ce ordine -------------------------------------------------


def roles_for(obligation_kinds: Iterable[str]) -> tuple[str, ...]:
    """Ordinea rolurilor, derivată din OBLIGAȚIILE turului (`plan.obligations`), nu o listă fixă.

    Contează pe un tur factual. La „care e prețul?" obligația e `answer`, nu `recommend`: cinci
    îngustări sub un răspuns punctual arată ca un bot care nu te-a auzit și te pune iar să alegi.
    Acolo continuarea firească e pe produsul discutat (detaliu, recenzii, link), nu pe filtre.

    Rolurile ABSENTE din ordine nu se emit deloc. Asta e regula care face diferența, nu ordinea:
    `answer` pur nu primește `forward`.
    """
    kinds = {str(k) for k in obligation_kinds}
    if kinds & {"recommend", "routine"}:
        return (ROLE_FORWARD, ROLE_LATERAL, ROLE_DEEPEN, ROLE_COMMIT)
    if "compare" in kinds:
        return (ROLE_DEEPEN, ROLE_COMMIT, ROLE_LATERAL)
    if kinds & {"answer", "explain"}:
        return (ROLE_DEEPEN, ROLE_COMMIT, ROLE_LATERAL)
    # Clarificare / acțiune / tur fără obligații: clientul n-a ajuns încă la produse, deci
    # îngustarea e chiar ce îl duce mai departe.
    return (ROLE_FORWARD, ROLE_LATERAL, ROLE_DEEPEN)


def select(
    candidates: Iterable[ChipMove],
    *,
    slots: int,
    role_order: Sequence[str],
    offered_before: Iterable[str] = (),
) -> list[ChipMove]:
    """Cele `slots` mutări care ies la client: mix de roluri IMPUS, apoi dovadă.

    Umplerea „cea mai bună dovadă întâi" ar da cinci îngustări pe aceeași axă — adevărate și
    inutile ca set. Se ia deci pe rând din fiecare rol (round-robin pe `role_order`), ceea ce
    garantează structural că un tur de recomandare are și o cale laterală, nu doar filtre.

    Determinist: la dovadă egală ordonează `move_id`. Un chip care se schimbă între două randări
    ale aceluiași tur ar face imposibil dedupe-ul pe conversație.
    """
    seen = {str(m) for m in offered_before}
    by_kind: dict[str, list[ChipMove]] = {}
    for move in sorted(candidates, key=lambda m: (-m.evidence, m.move_id)):
        if move.move_id in seen or move.role not in role_order:
            continue
        pool = by_kind.setdefault(move.kind, [])
        if len(pool) < _MAX_PER_KIND:
            pool.append(move)

    # Round-robin și ÎN INTERIORUL rolului, nu doar între roluri. Fără el, mutările se ordonau pe
    # `(-evidence, move_id)`, iar la dovadă egală câștiga alfabetul: pe un tur cu trei carduri
    # ieșeau `compare`, `detail`, `link`, iar `reviews` nu apărea NICIODATĂ. Măsurat pe ieșirea
    # reală a feliei, nu dedus.
    by_role: dict[str, list[ChipMove]] = {role: [] for role in role_order}
    for role in role_order:
        # Felurile se iau în ordinea DOVEZII lor celei mai bune, nu alfabetic: pe un tur de
        # recomandare, «ten uscat» (420 de produse) e o continuare mai bună decât o bandă de
        # preț care desparte un singur card, iar clientul citește primul chip, nu pe al cincilea.
        pools = sorted(
            (by_kind[k] for k in by_kind if MOVE_ROLES.get(k) == role),
            key=lambda pool: (-pool[0].evidence, pool[0].kind),
        )
        for rank in range(_MAX_PER_KIND):
            for pool in pools:
                if rank < len(pool):
                    by_role[role].append(pool[rank])

    picked: list[ChipMove] = []
    rank = 0
    while len(picked) < slots and any(len(v) > rank for v in by_role.values()):
        for role in role_order:
            pool = by_role[role]
            if rank < len(pool) and len(picked) < slots:
                picked.append(pool[rank])
        rank += 1
    return picked


# --- promptul și poarta ------------------------------------------------------------------------


def offer_block(moves: Sequence[ChipMove], pack: object, locale: str, heading: str) -> str:
    """Blocul de prompt cu mutările oferite. Gol dacă niciuna nu e reformulabilă.

    Fiecare rând poartă `move_id` și ancora ÎN GHILIMELE: modelul are voie să îmbrace fraza, dar
    ancora trebuie să supraviețuiască, iar poarta de mai jos o verifică. Șablonul serverului NU
    intră în prompt: dat ca exemplu, modelul l-ar copia, și am plăti tokeni ca să primim înapoi
    exact textul pe care îl aveam deja.
    """
    rows = [m for m in moves if m.rephrasable and m.anchor]
    if not rows:
        return ""
    lines = [f"  {m.move_id} [{m.role}] „{m.anchor}”" for m in rows]
    return f"{heading}\n" + "\n".join(lines) + "\n"


def apply_labels(
    moves: Sequence[ChipMove],
    labels: Mapping[str, str],
    pack: object,
    locale: str,
) -> tuple[list[str], dict[str, int]]:
    """Textele finale + un contor de diagnostic `{motiv: n}`.

    Ordinea e cea a mutărilor (decisă de `select`), nu cea a modelului: un model care își
    reordonează reformulările ar rearanja tăcut un mix de roluri pe care codul l-a ales.

    Trei motive de respingere, numărate separat fiindcă spun lucruri diferite: `no_anchor` =
    modelul a pierdut ancora (reformulare prea liberă), `too_long` = ar fi trebuit trunchiat DUPĂ
    verificare, `unknown_move` = a inventat un `move_id`. Toate trei cad pe șablon, deci clientul
    nu pierde niciun chip.
    """
    known = {m.move_id for m in moves}
    stats: dict[str, int] = {}

    def _count(reason: str) -> None:
        stats[reason] = stats.get(reason, 0) + 1

    for move_id in labels:
        if move_id not in known:
            _count("unknown_move")

    out: list[str] = []
    for move in moves:
        template_text = render_move(move, pack, locale)
        raw = labels.get(move.move_id) if move.rephrasable else None
        text = " ".join(str(raw).split()) if raw else ""
        if text:
            if len(text) > MAX_CHIP_LEN:
                _count("too_long")
                text = ""
            elif not contains_run(words_of(text), words_of(move.anchor)):
                _count("no_anchor")
                text = ""
            else:
                _count("rephrased")
        chip = text or template_text
        if chip and chip not in out:
            out.append(chip)
    return out, stats
