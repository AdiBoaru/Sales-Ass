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

  choose_within / fit_question ← fațetele PARTIȚIONANTE ale setului afișat (NX-316 felia 2)

Nu există aici o mutare „alternative din graful de relații", deși planul inițial o avea: ar cere
o interogare în plus pe drumul SINCRON, iar rolul ei (lateral) e deja acoperit de `pivot_shelf`,
care se compune din meniu fără niciun I/O nou. Se poate adăuga când există o măsurătoare care
arată că lipsește ceva, nu înainte (D15).

**NX-316 felia 2 — mutări PESTE setul afișat.** La iZi chips-urile sunt întrebări legate de ce
tocmai s-a arătat („ce aleg dintre acestea", „e potrivită pentru ten sensibil"); la noi niciun fel
nu lucra pe set. `choose_within` (forward) oferă câte o valoare a fațetei partiționante nerostite pe
care setul se desparte, aleasă de ACEEAȘI regulă ca întrebarea de îngustare NX-315
(`narrowing_candidate`), deci chips-urile și întrebarea numesc aceleași valori. `fit_question`
(deepen) întreabă de o valoare pe un produs doar când fișa lui are fațeta CUNOSCUTĂ: pe `UNKNOWN`
răspunsul ar fi „nu știu", iar un „nu" cunoscut e un răspuns bun. Frazele vin din meniul închis
(`value_phrases`, cu round-trip), deci modulul rămâne pur: apelantul le aduce.

**Copy-ul nu e în cod.** Șabloanele trăiesc în `DomainPack.chip_templates` (per locale), fiindcă
limba e configurație, nu constantă (P11). Pachet fără șablon pentru o mutare ⇒ mutarea nu se
oferă. Alternativa — o frază românească scrisă aici ca fallback — ar fi exact ce interzice P11.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from src.agent.fallbacks import fit_template
from src.catalog.clarify_menu import contains_run, words_of
from src.catalog.render_text import display_name, unique_prefixes
from src.catalog.vocabulary import CATEGORY_DIMENSION
from src.models import MAX_CHIP_LEN

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping, Sequence

    from src.catalog.clarify_menu import ClarifyMenu

__all__ = [
    "MOVE_ROLES",
    "ChipMove",
    "FacetPlan",
    "apply_labels",
    "choose_within_move",
    "drop_dead",
    "facet_move_parts",
    "facet_moves_for",
    "fit_question_move",
    "from_cards",
    "from_facets",
    "from_menu",
    "offer_block",
    "partitioning_sources",
    "plan_facets",
    "renderable",
    "render_move",
    "roles_for",
    "select",
    "wanted_phrases",
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
    # NX-316 felia 2: produse doar sub `CHIP_MOVES_V2_ENABLED` (apelantul nu cheamă `from_facets`
    # cu flagul stins), deci registrul extins nu schimbă nimic pe traficul de azi.
    "choose_within": ROLE_FORWARD,
    "fit_question": ROLE_DEEPEN,
}

#: NX-316: felurile care, în rolul lor, trec ÎNAINTEA celorlalte indiferent de dovadă. Dovada lor
#: e numărul de produse AFIȘATE (≤ `card_slots`), pe când a unei îngustări din meniu e numărul din
#: CATALOG (sute), deci o comparație directă le-ar îngropa mereu. Pe un tur cu carduri, „aleg
#: dintre acestea" e continuarea mai bună decât încă un filtru (cardul, „Rolurile").
_KIND_PRIORITY: dict[str, int] = {"choose_within": 0, "fit_question": 0}

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
    #: NX-318: sub câte cuvinte nu are voie să coboare fiecare slot la scurtare, fiindcă ar deveni
    #: prefixul altui produs de pe ecran. Gol ⇒ doar pragul general `_MIN_ANCHOR_WORDS`.
    floors: tuple[tuple[str, int], ...] = ()

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


#: Câte cuvinte din numele unui produs mai pot identifica UNIC produsul. Aceeași valoare și
#: același motiv ca `compose._MIN_NAME_WORDS`: sub două rămâne doar brandul, iar catalogul are
#: «Petala Nourish», «Petala Rich», «Petala Matte».
_MIN_ANCHOR_WORDS = 2


def _intact(text: str, values: Iterable[str]) -> bool:
    """Apar TOATE valorile de slot întregi în textul randat?"""
    haystack = words_of(text)
    return all(contains_run(haystack, words_of(v)) for v in values if v)


def _fit_anchor(
    move: ChipMove, pack: object, locale: str, *, floors: Mapping[str, int] | None = None
) -> ChipMove | None:
    """Mutarea cu SLOTURILE scurtate cât să încapă întregi în chip, sau `None` dacă nu se poate.

    Găsit rulând proba pe catalogul SOLE: numele scurt al unui produs real are ~33 de caractere
    («RIEMANN P20 Urban Shield SPF 50+»), iar «Spune-mi mai multe despre {slot}» are 25. Suma
    trece de 56, deci `fit_template` tăia numele, ancora nu se mai regăsea în text, și mutarea era
    aruncată. Efectul măsurat: pe un tur factual NU rămânea nicio continuare de tip „adâncire" —
    exact clasa cea mai utilă acolo.

    Scurtarea e pe CUVINTE ÎNTREGI, nu pe caractere, și păstrează minimum două: un prefix de două
    cuvinte din numele unui produs afișat e suficient ca `reference_resolver` să-l regăsească
    (aceeași regulă ca `compose._mention_index`), pe când «RIEMANN P20 Urban Shi…» nu e nici nume,
    nici prefix. Dacă nici două cuvinte nu încap, mutarea nu se oferă.

    Se scurtează TOATE sloturile, nu doar ancora, și asta a ieșit tot din probă: la comparație
    ancora (primul nume) supraviețuia, iar al doilea produs pleca trunchiat — «Compara BEAUTY OF
    JOSEON Relief Sun cu PURITO Daily Sof…». Verificarea doar pe ancoră spunea „e bine" despre un
    chip în care jumătate din promisiune era ilizibilă. Se taie mereu cel mai LUNG slot.

    NX-318: două cuvinte nu sunt „suficiente” în absolut, ci față de ce ALTCEVA e pe ecran. Pe
    turul real, «IT'S SKIN The Fresh» era prefixul a două produse (Blueberries, Coconut), iar
    apăsarea cădea pe o clarificare. Fiecare slot are deci pragul lui (`move.floors`, din
    `unique_prefixes`): se taie până la el, nu mai jos. Dacă nici așa nu încape, mutarea nu se
    oferă, fiindcă un chip ambiguu e mai rău decât unul lipsă.
    """
    values = dict(move.slots)
    limits = dict(move.floors) if floors is None else dict(floors)
    for _ in range(64):  # mărginit: fiecare trecere scoate un cuvânt dintr-un slot
        candidate = ChipMove(
            kind=move.kind,
            move_id=move.move_id,
            anchor=values.get("slot", move.anchor),
            slots=tuple(sorted(values.items())),
            evidence=move.evidence,
            floors=move.floors,
        )
        text = render_move(candidate, pack, locale)
        if text is not None and _intact(text, values.values()):
            return candidate
        trimmable = {
            k: v
            for k, v in values.items()
            if len(v.split()) > max(_MIN_ANCHOR_WORDS, limits.get(k, 0))
        }
        if not trimmable:
            return None
        key = max(trimmable, key=lambda k: (len(values[k]), k))
        values[key] = " ".join(values[key].split()[:-1])
    return None


def renderable(
    moves: Iterable[ChipMove],
    pack: object,
    locale: str,
    *,
    stats: dict[str, int] | None = None,
) -> list[ChipMove]:
    """Doar mutările pe care le putem EXPRIMA, cu ancora ajustată la ce va apărea EFECTIV în text.

    Se aplică ÎNAINTE de selecție, nu după, iar diferența e un slot pierdut: filtrată la randare,
    o mutare aleasă și apoi aruncată lăsa patru chips acolo unde catalogul avea cinci de oferit.

    Întoarce mutări posibil MODIFICATE (ancoră scurtată), nu doar filtrate, fiindcă ancora e ce
    judecă poarta: dacă textul emis conține un prefix, poarta trebuie să ceară acel prefix, nu
    numele întreg pe care nimeni nu-l va scrie.

    `stats`, dat, primește `dropped_ambiguous_anchor`: mutările care ar fi încăput cu pragul vechi
    de două cuvinte și au fost refuzate fiindcă ancora scurtată ar fi numit două produse (NX-318).
    """
    out: list[ChipMove] = []
    for move in moves:
        text = render_move(move, pack, locale)
        if text is not None and _intact(text, dict(move.slots).values()):
            out.append(move)
            continue
        fitted = _fit_anchor(move, pack, locale)
        if fitted is not None:
            out.append(fitted)
        elif stats is not None and move.floors and _fit_anchor(move, pack, locale, floors={}):
            stats["dropped_ambiguous_anchor"] = stats.get("dropped_ambiguous_anchor", 0) + 1
    return out


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
    cards: Sequence[Mapping[str, Any]],
    *,
    offered_before: Iterable[str] = (),
    unique_anchor: bool = False,
    locale: str | None = None,
) -> list[ChipMove]:
    """Mutările născute din ce tocmai s-a ARĂTAT: detaliu, recenzii, comparație, link, preț.

    Ancora e numele SCURT al produsului (`display_name`, aceeași regulă ca peste tot), fiindcă
    numele întreg din catalogul real are ~190 de caractere și n-ar încăpea într-un chip. Un card
    fără id sau fără nume nu produce mutări: un chip care numește un produs pe care nu-l putem
    rezolva la apăsare e fix defectul pe care îl evităm.

    `unique_anchor` (NX-318, `UNIQUE_NAME_PREFIX_ENABLED`): fiecare slot primește ca prag lungimea
    prefixului UNIC al produsului în setul cardurilor, ca `_fit_anchor` să nu-l scurteze până devine
    numele altui produs de pe ecran.
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
    unique = unique_prefixes(dict(named), locale=locale) if unique_anchor else {}

    def _add(
        kind: str,
        key: str,
        anchor: str,
        slots: dict[str, str],
        evidence: int,
        floors: dict[str, int] | None = None,
    ) -> None:
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
                floors=tuple(sorted((floors or {}).items())),
            )
        )

    def _floor(pid: str) -> int:
        return len(unique.get(pid, ()))

    for pid, short in named:
        floors = {"slot": _floor(pid)} if unique else None
        _add("detail", pid, short, {"slot": short}, 1, floors)
        _add("reviews", pid, short, {"slot": short}, 1, floors)
        _add("link", pid, short, {"slot": short}, 1, floors)
    if len(named) >= 2:
        (id_a, a), (id_b, b) = named[0], named[1]
        # Ancora comparației e PRIMUL nume: `fit_template` poate scurta al doilea slot ca să încapă,
        # iar o ancoră pe un text scurtat n-ar mai fi verificabilă.
        floors = {"slot": _floor(id_a), "slot_b": _floor(id_b)} if unique else None
        _add("compare", f"{id_a}:{id_b}", a, {"slot": a, "slot_b": b}, 2, floors)
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


def drop_dead(
    moves: Iterable[ChipMove],
    *,
    spoken_needs: Iterable[tuple[str, str]] = (),
    n_cards: int = 0,
) -> tuple[list[ChipMove], int]:
    """NX-316: scoate mutările ADEVĂRATE dar MOARTE, înainte de selecție. `(păstrate, câte scoase)`.

    Două clase, amândouă văzute pe conversația reală comparată cu iZi:

    - `refine_facet` pe o nevoie pe care clientul a ROSTIT-o deja: «Caut ceva pentru hidratare»
      sub răspunsul la «vreau o cremă de hidratare». Dovada nu e o listă de cuvinte, ci perechea
      `(dimensiune, cheie)` din subiectul conversației (NX-314), unde intră doar nevoile coroborate
      de client și rezolvate pe fațete KNOWN. Deci merge pe orice limbă și orice vertical (P11).
    - `detail` pe un tur cu UN singur card: cardul are deja butonul lui de detalii, iar chip-ul ar
      repeta același lucru într-un slot care putea duce altundeva.

    Scoase ÎNAINTE de `select`, nu după, din motivul din `renderable`: o mutare aleasă și apoi
    aruncată ar lăsa un slot gol acolo unde exista o continuare bună.
    """
    spoken = {(str(d), str(k)) for d, k in spoken_needs}
    kept: list[ChipMove] = []
    dropped = 0
    for move in moves:
        if move.kind == "refine_facet":
            _, dimension, key = (move.move_id.split(":", 2) + ["", ""])[:3]
            if (dimension, key) in spoken:
                dropped += 1
                continue
        if move.kind == "detail" and n_cards == 1:
            dropped += 1
            continue
        kept.append(move)
    return kept, dropped


# --- NX-316 felia 2: mutări peste setul AFIȘAT, pe fațetele lui partiționante --------------------


@dataclass(frozen=True, slots=True)
class FacetPlan:
    """Ce pot oferi mutările pe fațete, ÎNAINTE de fraze. Pur, calculat din cardurile turului.

    Există ca pas separat fiindcă frazele (`value_phrases`) cer vocabularul, adică I/O: apelantul
    află din `wanted()` ce chei are de frazat, le aduce, apoi `from_facets` construiește mutările.
    Calculat de două ori, planul ar putea numi alte chei decât cele pentru care s-au adus fraze.

    `values` = cheile fațetei `facet` prezente în set, descrescător după câte produse AFIȘATE le
    poartă (dovada lui `choose_within`). `fit_values` = perechile `(fațetă, cheie)` pe care
    `fit_question` le poate întreba, în ordinea preferinței: întâi ce a ROSTIT clientul, apoi
    valorile de îngustare.
    """

    facet: str | None = None
    values: tuple[tuple[str, int], ...] = ()
    fit_values: tuple[tuple[str, str], ...] = ()

    def wanted(self) -> dict[str, tuple[str, ...]]:
        out: dict[str, list[str]] = {}
        if self.facet:
            out.setdefault(self.facet, []).extend(k for k, _ in self.values)
        for facet, key in self.fit_values:
            out.setdefault(facet, []).append(key)
        return {f: tuple(dict.fromkeys(keys)) for f, keys in out.items()}


def partitioning_sources(facets: Iterable[object]) -> dict[str, str]:
    """`{cheia fațetei: cheia din attributes}` pentru fațetele pe care se poate alege dintre."""
    out: dict[str, str] = {}
    for facet in facets:
        if getattr(facet, "binding", "additive") != "partitioning":
            continue
        if getattr(getattr(facet, "source", None), "value", None) != "attribute":
            continue
        key = str(getattr(facet, "key", "") or "")
        if key:
            out[key] = str(getattr(facet, "source_key", key) or key)
    return out


def plan_facets(
    cards: Sequence[Mapping[str, Any]],
    facets: Sequence[object],
    *,
    known: frozenset[str] = frozenset(),
    spoken: Iterable[tuple[str, str]] = (),
) -> FacetPlan:
    """Fațeta pe care setul afișat se DESPARTE + valorile de întrebat pe produse. PUR.

    Fațeta o alege `narrowing_candidate` (NX-315), fără prag de câștig: întrebarea de îngustare
    are nevoie de prag fiindcă ocupă fraza de final a răspunsului, un chip nu ocupă decât un slot.
    Aceeași regulă de alegere ⇒ pe un tur cu întrebare, chips-urile numesc exact valorile ei.

    O valoare ROSTITĂ pe care o poartă TOATE cardurile nu se întreabă: setul a fost găsit chiar
    pe ea (clientul a cerut „ten uscat", căutarea a filtrat pe `dry`), deci „merge pentru ten
    uscat?" ar avea mereu răspunsul „da". E un chip mort, din aceeași clasă ca `drop_dead`.
    """
    from src.conversation.clarification_policy import (  # noqa: PLC0415 — evită ciclul
        narrowing_candidate,
        values_of,
    )

    if not cards:
        return FacetPlan()
    by_key = partitioning_sources(facets)
    verdict = narrowing_candidate(facets, cards, known=known, min_gain=0.0)
    offer = verdict.offer
    facet = offer.facet if offer is not None else None
    values = tuple(zip(offer.values, offer.partition, strict=True)) if offer is not None else ()

    source_to_key = {src: key for key, src in by_key.items()}
    fit: list[tuple[str, str]] = []
    for dimension, value in spoken:
        key = str(dimension) if str(dimension) in by_key else source_to_key.get(str(dimension))
        if key is None:
            continue
        value = str(value)
        if all(value in values_of(c, by_key[key]) for c in cards):
            continue
        fit.append((key, value))
    if facet is not None:
        fit.extend((facet, v) for v, _ in values)
    return FacetPlan(facet=facet, values=values, fit_values=tuple(dict.fromkeys(fit)))


def choose_within_move(facet: str, key: str, phrase: str, evidence: int) -> ChipMove:
    """„Pentru ten uscat, ce aleg dintre acestea?" — o valoare a fațetei, peste setul afișat."""
    return ChipMove(
        kind="choose_within",
        move_id=f"choose_within:{facet}:{key}",
        anchor=phrase,
        slots=(("slot", phrase),),
        evidence=evidence,
    )


def fit_question_move(
    product_id: str, short_name: str, facet: str, key: str, phrase: str, *, floor: int = 0
) -> ChipMove:
    """„X merge pentru ten uscat?" — ancora e fraza valorii, iar numele e al doilea slot.

    Fraza nu are voie să fie scurtată (pragul ei e lungimea întreagă): o valoare trunchiată
    («ten») s-ar rezolva pe altă cheie decât cea întrebată. Numele cedează primul, până la prefixul
    lui unic pe ecran (NX-318)."""
    return ChipMove(
        kind="fit_question",
        move_id=f"fit_question:{product_id}:{facet}:{key}",
        anchor=phrase,
        slots=(("slot", phrase), ("slot_b", short_name)),
        evidence=1,
        floors=(("slot", len(phrase.split())), ("slot_b", floor)),
    )


#: Pe câte carduri (primele, în ordinea de pe ecran) se oferă `fit_question`. Primul e produsul
#: discutat; al doilea acoperă turul de recomandare. Fără limită, `select` ar alege între carduri
#: după `move_id`, adică după un uuid.
_FIT_CARDS = 2


def _names(
    cards: Sequence[Mapping[str, Any]], *, unique_anchor: bool, locale: str | None
) -> list[tuple[str, str, int]]:
    """`(product_id, nume scurt, prag de cuvinte)` în ordinea de pe ecran — aceeași regulă ca
    `from_cards`, ca recunoașterea să re-randeze exact textul emis."""
    named = [
        (str(c.get("product_id") or c.get("id")), display_name(str(c.get("name"))))
        for c in cards
        if (c.get("product_id") or c.get("id")) and c.get("name")
    ]
    unique = unique_prefixes(dict(named), locale=locale) if unique_anchor else {}
    return [(pid, short, len(unique.get(pid, ()))) for pid, short in named]


def from_facets(
    cards: Sequence[Mapping[str, Any]],
    plan: FacetPlan,
    facets: Sequence[object],
    phrases: Mapping[tuple[str, str], str],
    *,
    offered_before: Iterable[str] = (),
    unique_anchor: bool = False,
    locale: str | None = None,
) -> list[ChipMove]:
    """Mutările `choose_within` + `fit_question` ale turului. PUR.

    O valoare fără frază nu se oferă (fail-closed pe text, ca tot modulul): fraza e cea care, la
    apăsare, se rezolvă înapoi pe cheie. `fit_question` cere fațeta CUNOSCUTĂ pe produs, fiindcă
    răspunsul vine din fișă, iar pe o fișă fără valoare ar fi „nu știu"."""
    from src.conversation.clarification_policy import values_of  # noqa: PLC0415

    seen = {str(m) for m in offered_before}
    by_key = partitioning_sources(facets)
    out: list[ChipMove] = []
    if plan.facet is not None:
        for key, count in plan.values:
            phrase = phrases.get((plan.facet, key))
            if phrase:
                out.append(choose_within_move(plan.facet, key, phrase, count))
    for pid, short, floor in _names(cards, unique_anchor=unique_anchor, locale=locale)[:_FIT_CARDS]:
        card = next(c for c in cards if str(c.get("product_id") or c.get("id")) == pid)
        for facet, key in plan.fit_values:
            phrase = phrases.get((facet, key))
            if not phrase or facet not in by_key or not values_of(card, by_key[facet]):
                continue
            out.append(fit_question_move(pid, short, facet, key, phrase, floor=floor))
            break
    return [m for m in out if m.move_id not in seen]


def facet_move_parts(move_id: str) -> tuple[str, str | None, str, str] | None:
    """`(fel, product_id | None, fațetă, cheie)` pentru o mutare pe fațete, altfel `None`."""
    kind, _, rest = str(move_id).partition(":")
    parts = rest.split(":")
    if kind == "choose_within" and len(parts) == 2 and all(parts):
        return kind, None, parts[0], parts[1]
    if kind == "fit_question" and len(parts) == 3 and all(parts):
        return kind, parts[0], parts[1], parts[2]
    return None


def facet_moves_for(
    offered: Iterable[str],
    cards: Sequence[Mapping[str, Any]],
    phrases: Mapping[tuple[str, str], str],
    *,
    unique_anchor: bool = False,
    locale: str | None = None,
) -> list[ChipMove]:
    """Mutările pe fațete OFERITE, reconstruite din `move_id` + setul afișat + fraze, pentru
    recunoașterea apăsării. Dovada nu contează aici (mutarea a fost deja oferită), iar numele
    și pragurile vin din ACEEAȘI funcție ca la emitere, deci textul re-randat e cel emis."""
    names = {
        pid: (short, floor)
        for pid, short, floor in _names(cards, unique_anchor=unique_anchor, locale=locale)
    }
    out: list[ChipMove] = []
    for move_id in offered:
        parts = facet_move_parts(move_id)
        if parts is None:
            continue
        kind, pid, facet, key = parts
        phrase = phrases.get((facet, key))
        if not phrase:
            continue
        if kind == "choose_within":
            out.append(choose_within_move(facet, key, phrase, 1))
        elif pid in names:
            short, floor = names[pid]
            out.append(fit_question_move(pid, short, facet, key, phrase, floor=floor))
    return out


def wanted_phrases(offered: Iterable[str]) -> dict[str, tuple[str, ...]]:
    """Ce fraze trebuie aduse ca să recunoaștem mutările pe fațete oferite. Gol ⇒ nicio citire."""
    out: dict[str, list[str]] = {}
    for move_id in offered:
        parts = facet_move_parts(move_id)
        if parts is not None:
            out.setdefault(parts[2], []).append(parts[3])
    return {f: tuple(dict.fromkeys(keys)) for f, keys in out.items()}


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
    overflow: list[ChipMove] = []
    for move in sorted(candidates, key=lambda m: (-m.evidence, m.move_id)):
        if move.move_id in seen or move.role not in role_order:
            continue
        pool = by_kind.setdefault(move.kind, [])
        if len(pool) < _MAX_PER_KIND:
            pool.append(move)
        else:
            overflow.append(move)

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
            key=lambda pool: (
                _KIND_PRIORITY.get(pool[0].kind, 1),
                -pool[0].evidence,
                pool[0].kind,
            ),
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

    # Al doilea tur: umple ce a rămas cu ce a fost tăiat de `_MAX_PER_KIND`.
    #
    # Plafonul pe fel exprimă o PREFERINȚĂ pentru diversitate, nu o limită de adevăr — iar tratat
    # ca limită dură înfometa exact turul care are cea mai mare nevoie de sugestii. Măsurat pe
    # catalogul SOLE: la PRIMUL tur, fără raft discutat, meniul nu poate oferi fațete (NX-295: o
    # fațetă neancorată pe raft e o promisiune falsă), deci singurul fel disponibil e `pivot_shelf`
    # — și ieșeau 2 chips din 5, tocmai când clientul are cel mai puțin context.
    #
    # Rămâne o preferință fiindcă overflow-ul intră DUPĂ ce fiecare rol și-a spus cuvântul: un al
    # doilea raft nu poate lua locul unei căi de adâncire care există.
    for move in overflow:
        if len(picked) >= slots:
            break
        picked.append(move)
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
