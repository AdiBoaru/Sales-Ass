"""Kernel `kernel.v1.1`, pasul 4b (NX-333) — plannerul turului. PUR.

Contractul (§„Ownership", rândurile `SearchArgs` și „Which executor runs"): modelul nu scrie
niciodată `SearchArgs` și nu alege executorul; plannerul le decide pe amândouă. Pe calea
interpretată modulul ăsta e SINGURUL loc din kernel care construiește `SearchArgs` (I2, poarta AST
din `tests/test_kernel_contract.py`), iar singurul lui constructor e `_Planner._search_args`.

Intrările sunt ieșirile straturilor de dinainte, niciodată textul interpretării:

- `state` = starea DUPĂ reducer (subiectul, nevoile, ecranul de dinaintea turului, `active_search`);
- `ranking` = semnalele `inferred` ale turului (`TurnDelta.ranking`, I23: nepersistate);
- `resolved` = referințele rezolvate de resolver, revalidate pe catalog (I1: singura sursă de id-uri
  a unui plan);
- `gate` = verdictul porții de ambiguitate (NX-332), cu actele scoase de regula 0.

Ordinea e fixă (P1): verdictul porții → tabelul act → executor → constructorul de `SearchArgs`.
`Act.query` e citit O DATĂ, ca VALOARE trecută mai departe (`_read_act_query`, cititor declarat în
`tests/kernel_contract_allowlist.json`), niciodată ca ramificare.

Ce nu încape în `SearchArgs` nu se pierde tăcut și nu se ghicește: intră în `PlannedTurn.gaps`
(vocabular ÎNCHIS, `GAPS`), deci trace-ul arată ce n-a ajuns în căutare. Ce trebuie spus
clientului („nu am găsit exact", un act nefăcut) intră în `PlannedTurn.disclosures`, ca un cod
închis; textul îl scrie compunerea (pasul 6).

Ce NU face: nu execută nimic (pasul 6), nu cheamă modelul (I13), nu scrie stare (I3), nu citește
id-uri din `state.references`, din parcat sau din `recent_sets` (I1: doar prin `resolved`), nu
conține literali de vertical (I14)."""

from __future__ import annotations

import math
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass

from src.agent.tool_budget import spec_for
from src.agent.tool_definitions import TOOL_NAMES
from src.catalog.query_terms import (
    stopwords,
    tokens,
)
from src.catalog.vocabulary import (
    CatalogVocabulary,
    topic_root_of,
)
from src.conversation.ambiguity_gate import GateOutcome, target_question_key
from src.conversation.answer_policy import read_query
from src.conversation.delta import RankingSignal
from src.conversation.interpretation import (
    Act,
    CheckedChange,
    Executor,
    Reference,
    ResolvedRef,
    StateChange,
    TurnInterpretation,
    TurnPlan,
)
from src.conversation.needs import (
    HARD,
    MAX_UNMAPPED_PER_TOPIC,
    PRICE_BOUNDS,
    PRICE_DIMENSION,
    UNMAPPED_KEY,
    NeedKind,
    NeedVocabulary,
    norm_text,
)
from src.conversation.provenance import hard_capable
from src.conversation.references import (
    BRAND_DIMENSION,
    MUTATING_ACTS,
    RATING_DIMENSION,
    VARIANT_DIMENSION,
)
from src.conversation.state_v2 import HARD_CAPABLE_SOURCES, ConversationStateV2, Need, Topic
from src.domain.facets import FacetType
from src.domain.routine_steps import SEP
from src.tools.base import CATALOG_READ_TOOLS
from src.tools.catalog_tools import SearchArgs

#: Contractul, „Multi-act turns": cel mult două executoare pe tur.
MAX_PLANS = 2
#: Indexul unei dezvăluiri care nu ține de un plan (un act nefăcut): e a turului.
TURN_LEVEL = -1

#: Vocabularul ÎNCHIS al lui `PlannedTurn.disclosures`. Codul, nu textul: fraza e a compunerii.
DISCLOSURES: tuple[str, ...] = (
    "not_exact_match",  # căutarea e plasa unui nume negăsit sau a unei ținte dispărute
    "invalid_target",  # actul a fost scos de regula 0 a porții (țintă nedeclarată / proprietate)
    "dropped_act",  # al treilea act al turului, raportat ca nefăcut
    "no_target",  # un act care cere o țintă a rămas fără niciuna
)
#: Vocabularul ÎNCHIS al lui `PlannedTurn.gaps`: ce n-a putut purta `SearchArgs`, plus coborârile
#: care nu sunt tăcere (P6): o întrebare fără text, o căutare fără cuvinte, un subiect lipsă.
GAPS: tuple[str, ...] = (
    "soft_budget",  # buget `soft` / `inferred`: niciodată `price_max` (I7)
    "price_min",  # `SearchArgs` n-are `price_min`
    "exclusion",  # `SearchArgs` n-are excluderi
    "numeric_facet",  # un prag pe o fațetă numerică (NX-334: `<fațetă>_min` / `_max`)
    "variant",  # o variantă cerută și negăsită: starea nu ține o variantă
    "unsupported_need",  # o nevoie fără câmp și fără fațetă de atribut (mărime, destinatar, bool)
    "soft_brand",  # o marcă preferată, iar marca nu e un atribut al produselor (fuziunea n-o vede)
    "subject_type",  # tipul subiectului, iar pachetul n-are fațeta de tip (nu are ce ordona)
    "no_query",  # nicio cerere, niciun subiect, niciun nume: căutarea n-are ce căuta
    "no_question",  # poarta a cerut o întrebare, dar n-avea cu ce s-o scrie
    "no_subject",  # `find` fără subiect, cuvinte sau nevoi, iar poarta n-a întrebat
    # D3 (`kernel.v2.1`): bugetul CONVERSAȚIEI e al unui produs; pe o rutină e un plafon pe SUMA
    # pașilor, deci intră doar o sumă spusă în turul rutinei
    "routine_budget",
    # D3: un câmp al cererii fără corespondent într-o rutină (sortare, termeni nemapați)
    "routine_field",
)

#: Uneltele buclei delegate (actul `other`): toate, fără cele care citesc catalogul și fără
#: mutații. Derivat din TREI registre, nu scris de mână: schemele (`TOOL_NAMES`), citirile de
#: catalog (`CATALOG_READ_TOOLS`) și clasificarea read/mutation (`tool_budget`). `search_products`/
#: `routine_plan` ar însemna argumente de căutare scrise de model (I2); `get_product_details`/
#: `compare_products`/`related_products`/`reorder` produse alese de model în afara resolverului
#: (I1); `cart_add`/`checkout_link`/`subscribe_back_in_stock` un `product_id` ales de model pe o
#: MUTAȚIE (I1 + I10: o mutație cere o țintă `exact` a resolverului). Cablarea buclei e a pasului 6.
DELEGATE_TOOLS: frozenset[str] = frozenset(
    name for name in TOOL_NAMES if name not in CATALOG_READ_TOOLS and not spec_for(name).is_mutation
)

#: Actele de citire care cer o țintă; pe un nume negăsit devin căutare (contractul + NX-333).
_READ_ACTS: frozenset[str] = frozenset({"detail", "link", "compare"})
#: Actele fără țintă, cu executorul lor fix.
_FIXED: Mapping[str, Executor] = {
    "store_info": "faq",
    "order_status": "order",
    "chitchat": "reply_only",
    "other": "delegate",
}
_SEARCHING: frozenset[str] = frozenset({"search", "bundle"})
#: Cheia de pachet care servește `bundle` pe orice raft.
WILDCARD = "*"
#: Dimensiunea tipului de produs (`Topic.product_type`), eticheta subiectului fără raft.
PRODUCT_TYPE = "product_type"
_BUDGET_MIN, _BUDGET_MAX = PRICE_BOUNDS


@dataclass(frozen=True)
class PlannedTurn:
    """Planul turului. `plans` are cel mult `MAX_PLANS` intrări, mutația înaintea citirii;
    `primary` = indexul planului actului principal (ultimul act nescos). `disclosures` = (index
    plan, cod din `DISCLOSURES`), cu `TURN_LEVEL` pentru un act nefăcut; `gaps` = coduri din
    `GAPS`, fără duplicate, în ordinea apariției."""

    plans: tuple[TurnPlan, ...]
    dropped_acts: int = 0
    gaps: tuple[str, ...] = ()
    disclosures: tuple[tuple[int, str], ...] = ()
    primary: int = 0


def _read_act_query(
    query: str | None,
    has_words: bool,
    changes: Sequence[StateChange],
    checked: Sequence[CheckedChange] | None,
    carried: Mapping[str, str],
    type_label: str | None,
    shelf_label: str | None,
    shelf_names: Collection[str],
    locale: str,
    flags: Collection[str] = (),
) -> tuple[str | None, str | None]:
    """SINGURUL loc din planner care citește `Act.query` și citatele schimbărilor (cuvintele
    clientului). Întoarce `(textul căutării, cererea întreagă fără cuvintele negate)`, ca VALORI;
    nicio decizie nu ramifică pe text.

    NX-352 (sonda NX-351, trei recenzii): cererea întreagă nu e o căutare. Pe treapta `strict`
    fiecare cuvânt e o poartă (lecția NX-298), deci «si ceva mai ieftin ?» dădea zero produse, iar
    «pai mi se usuca pielea dupa dus» potriviri pe «dus». O SCĂDERE din frază (formula locale-i,
    nevoile) a picat la recenzie de două ori: orice cuvânt neprevăzut rămânea poartă. Deci textul se
    COMPUNE din ce a validat kernelul, în ordinea:
    1. TIPUL: cuvintele care l-au numit în tur (`CheckedChange.matched` al unui tip `explicit`),
       altfel eticheta tipului din stare (`type_label`: tipul spus, capul umbrelei);
    2. altfel RAFTUL spus de client: cuvintele care l-au numit (`explicit`), sau cuvintele citatului
       unui raft `implicit` care seamănă cu numele raftului (`shelf_names`: «telefon» pentru
       „Telefoane"); restul citatului nu, fiindcă poate fi o descriere («pielea mea care se
       înroșește»). Numele raftului nu apare în numele produselor (NX-293), deci cuvântul clientului
       e textul bun;
    3. fără tip și fără cuvântul clientului pentru raft: cuvintele unei fațete FILTRATE («anti
       aging»; un număr nu e text), altfel valorile nemapate («niacinamida»), altfel eticheta
       raftului (`shelf_label`);
    la fiecare, cuvintele unei valori SPUSE care nu ajunge filtru (o marcă pe o preferință slabă sau
    într-un gol), fiindcă textul e atunci canalul ei; 4. cererea întreagă, fără cuvintele negate, e
    ultima rezervă. Doar schimbările ACCEPTATE (`checked`) contează; `avoid` nu dă text. Cuvintele
    ies în ordinea din cerere, în forma scrisă de client.

    NX-349: citatul unei fațete da/nu (`flags`) nu dă text nici în rezervă: proprietatea o poartă
    fațeta (sau golul dezvăluit), iar cuvintele ei sunt adesea negația unui lucru («fără parfum»),
    deci «parfum» ar urca exact produsele parfumate."""
    stop = stopwords(locale)
    # (dimensiune, relație, proveniență, cuvintele care au numit valoarea, valoarea de text,
    # cuvintele citatului, cuvintele citatului în forma scrisă de client)
    items: list[tuple[str, str, str, tuple[str, ...], str | None, list[str], list[str]]] = []
    if checked is not None:
        for c in checked:
            if c.rejected is None and c.strength != "ranking":
                value = c.canonical_value if isinstance(c.canonical_value, str) else None
                raw = [w.strip(_PUNCTUATION) for w in (c.change.quote or "").split()]
                relation = c.change.relation or "eq"
                quote = tokens(c.change.quote) if c.change.quote else []
                items.append((c.dimension, relation, c.provenance, c.matched, value, quote, raw))
    else:
        for change in changes:
            raw = [w.strip(_PUNCTUATION) for w in (change.quote or "").split()]
            quote = tokens(change.quote) if change.quote else []
            relation = change.relation or "eq"
            items.append(
                (change.dimension, relation, "explicit", tuple(quote), change.value, quote, raw)
            )
    order = tokens(query or "")

    def surface(words: Sequence[str], raw: Sequence[str]) -> list[str]:
        """Cuvântul clientului doar când e ÎNTREG acel token («husă» pentru «husa»); dintr-un cuvânt
        compus («ten/fata») doar tokenul, nu și vecinul lui."""
        return [next((r for r in raw if tokens(r) == [w]), w) for w in words]

    def like_shelf(word: str) -> bool:
        """Cuvântul seamănă cu numele raftului: prefix comun de cel puțin lungimea celui mai scurt
        minus 2, minimum 3 litere (pluralul alternează: «telefon» / „telefoane",
        «husă» / „huse")."""

        def common(a: str, b: str) -> int:
            n = 0
            while n < min(len(a), len(b)) and a[n] == b[n]:
                n += 1
            return n

        return any(common(word, n) >= max(3, min(len(word), len(n)) - 2) for n in shelf_names)

    live = [i for i in items if i[1] not in _NEGATIVE]
    type_words: list[str] = []
    shelf_words: list[str] = []
    for dimension, _r, provenance, matched, _v, quote, raw in live:
        if dimension == PRODUCT_TYPE and provenance == "explicit":
            type_words += surface(matched, raw)
        elif dimension == "category" and provenance == "explicit":
            shelf_words += surface(matched, raw)
        elif dimension == "category":
            content = [w for w in quote if w not in stop]
            alike = [w for w in content if like_shelf(w)]
            # un citat SCURT fără cuvânt de raft e numele produsului («cremă de față»); unul
            # lung e o descriere («pielea mea care se înroșește») și nu intră în text
            trimmed = list(quote)
            while trimmed and trimmed[0] in stop:
                trimmed.pop(0)
            while trimmed and trimmed[-1] in stop:
                trimmed.pop()
            short = trimmed if len(content) <= _SHORT_QUOTE else []
            shelf_words += surface(alike or short, raw)
    spoken = [i for i in live if i[2] == "explicit" and i[0] not in _SUBJECT_WORDS]
    gapped = [
        w
        for d, _r, _p, m, v, _q, raw in spoken
        if v and d != UNMAPPED_KEY and carried.get(d) != "filter"
        for w in surface(m, raw)
    ]
    named_filters = [
        w
        for d, _r, _p, m, v, _q, raw in spoken
        if v and carried.get(d) == "filter"
        for w in surface(m, raw)
    ]
    unmapped = [v for d, _r, _p, _m, v, _q, _raw in live if d == UNMAPPED_KEY and v]
    negated = {w for d, r, _p, _m, _v, q, _raw in items if r in _NEGATIVE or d in flags for w in q}
    whole = None
    if has_words and query is not None:
        own = [w for w in query.split() if not set(tokens(w)) & negated]
        whole = " ".join(own).strip() or None

    def ordered(words: Sequence[str]) -> str:
        """În ordinea din cerere; o etichetă din stare (fără loc în cerere) rămâne întreagă."""
        unique = list(dict.fromkeys(w for w in words if w))

        def position(word: str) -> int:
            first = (tokens(word) or [""])[0]
            return order.index(first) if first in order else len(order)

        return " ".join(sorted(unique, key=position))

    head = type_words or ([type_label] if type_label else []) or shelf_words
    candidates = (
        [*head, *gapped] if head else [],
        [*named_filters, *gapped],
        [*unmapped, *gapped],
        [shelf_label, *gapped] if shelf_label else [],
    )
    for candidate in candidates:
        text = ordered(candidate)
        if text:
            return text, whole
    return None, whole


#: NX-352: dimensiunile ale căror cuvinte rămân în căutare chiar dacă un citat de nevoie le
#: cuprinde: numesc CE ESTE produsul. Un cuvânt nemapat nu e consumat de nimic (e textul căutat).
_SUBJECT_WORDS = frozenset({"category", PRODUCT_TYPE})
#: Câte cuvinte de conținut are cel mult citatul unui raft `implicit` ca să numească un produs.
_SHORT_QUOTE = 3
#: Relațiile care OCOLESC o valoare: cuvântul lor nu e niciodată text de căutare.
_NEGATIVE = frozenset({"avoid"})
_PUNCTUATION = ".,;:!?…\"'()[]«»„”“"
#: Cererea provizorie a argumentelor, înlocuită imediat cu textul compus (`query` nu e goală).
_PENDING = "·"


def _lang(locale: str) -> str:
    return (locale or "").strip().lower().split("-")[0]


class _Planner:
    def __init__(
        self,
        interp: TurnInterpretation,
        state: ConversationStateV2,
        ranking: Sequence[RankingSignal],
        resolved: Sequence[ResolvedRef],
        gate: GateOutcome,
        *,
        changed: bool,
        pack: object | None,
        vocab: CatalogVocabulary | None,
        locale: str,
        checked: Sequence[CheckedChange] | None = None,
    ) -> None:
        self.interp = interp
        self.checked = tuple(checked) if checked is not None else None
        self.state = state
        self.ranking = tuple(ranking)
        self.resolved = {r.ref_id: r for r in resolved}
        self.refs = {r.id: r for r in interp.references}
        self.gate = gate
        # „Schimbări în tur" (contractul): propunerile reducerului. În plus, două schimbări pe care
        # propunerile nu le poartă, dar după care pagina VECHE nu mai e răspunsul: o reluare
        # (`thread=resume` schimbă subiectul fără nicio propunere, iar sesiunea e a subiectului
        # parcat) și un semnal `inferred` al turului (ordinea s-a schimbat, I23). Recenzia NX-333:
        # «Înapoi la telefoane, mai arată-mi» pagina husele.
        self.changed = changed or interp.thread == "resume" or bool(self.ranking)
        self.pack = pack
        self.vocab = vocab if vocab is not None and not vocab.is_empty() else None
        self.locale = locale
        self.needs = NeedVocabulary.from_pack(pack)  # type: ignore[arg-type]
        self.facets = {
            f.key: f for f in getattr(pack, "facets", ()) or () if getattr(f, "key", None)
        }
        self.searchable = frozenset(getattr(pack, "searchable_facets", ()) or ())
        # NX-349: fațetele da/nu ale pachetului (citatul lor nu dă text de căutare)
        self.flags = frozenset(
            key
            for key, f in self.facets.items()
            if getattr(f, "value_type", None) is FacetType.BOOL
        )
        self.gaps: list[str] = []
        self.disclosures: list[tuple[int, str]] = []

    # --- utilitare -------------------------------------------------------------------------------

    def _gap(self, code: str) -> None:
        if code not in self.gaps:
            self.gaps.append(code)

    def _disclose(self, index: int, code: str) -> None:
        """O dezvăluire a TURULUI (un act nefăcut) se scrie o dată per act, ca răspunsul să le
        poată număra; una a unui plan, o singură dată."""
        if index == TURN_LEVEL or (index, code) not in self.disclosures:
            self.disclosures.append((index, code))

    def _refs_of(self, act: Act) -> list[ResolvedRef]:
        return [self.resolved[t] for t in act.targets if t in self.resolved]

    def _untargeted(self) -> list[Reference]:
        """Referințele pe care nu le țintește niciun act („cel mai ieftin" spus lângă o căutare)."""
        targeted = {t for a in self.interp.acts for t in a.targets}
        return [r for r in self.interp.references if r.id not in targeted]

    def _subject(self) -> bool:
        return self.state.topic.has_subject

    def _facet_needs(self) -> bool:
        return any(self.needs.dimension_of(n.key) in self.facets for n in self.state.active_needs())

    def _subject_label(self) -> str | None:
        """Eticheta subiectului, din date: tipul (umbrela, apoi tipul spus: etichetă de pachet,
        apoi de vocabular, apoi cheia), apoi raftul (`VocabEntry.label`). NX-352: eticheta e TEXTUL
        căutării când turul nu numește subiectul, iar numele tipului apare în numele produselor,
        pe când numele raftului nu (NX-293), deci tipul întâi. Workaroundul declarat în contract:
        `SearchArgs.query` cere ≥ 1 caracter, iar treapta `filters_only` servește cererea fără
        cuvinte."""
        topic = self.state.topic
        if topic.type_umbrella and (topic.type_learned or not topic.product_type):
            # NX-350: eticheta unei umbrele e CAPUL primului ei cod (cuvântul spus de client,
            # «cremă»), nu codul întreg: altfel căutarea fără cuvinte ar alege unul. Nu un cuvânt
            # din coadă, comun tuturor («fata» e raftul de machiaj, NX-319) și nici unul gol al
            # locale-i (recenziile NX-350, constatările 6). Un tip DEDUS nu o bate (constatarea 7).
            stop = stopwords(self.locale)
            words = [w for w in topic.type_umbrella[0].split() if w not in stop]
            return words[0] if words else topic.type_umbrella[0]
        if topic.product_type:
            getter = getattr(self.pack, "value_label", None)
            for loc in dict.fromkeys((self.locale, _lang(self.locale))):
                label = getter(PRODUCT_TYPE, topic.product_type, loc) if callable(getter) else None
                if label:
                    return label
            for entry in self.vocab.entries(PRODUCT_TYPE) if self.vocab else ():
                if entry.key == topic.product_type and entry.label:
                    return entry.label
            return topic.product_type
        return self._shelf_label()

    def _shelf_label(self) -> str | None:
        """Eticheta raftului subiectului (`VocabEntry.label`), altfel cheia; `None` fără raft."""
        key = self.state.topic.category_key
        if not key:
            return None
        for entry in self.vocab.categories if self.vocab else ():
            if entry.key == key and entry.label:
                return entry.label
        return key

    def _hard(self, need: Need, dimension: str) -> bool:
        """I7: dur doar ce e dur în stare, de la o sursă care poate susține un filtru, pe o
        dimensiune hard-capable (I8). Oricare lipsă ⇒ preferință, nu filtru."""
        return (
            need.strength == HARD
            and need.source in HARD_CAPABLE_SOURCES
            and hard_capable(dimension, self.pack)
        )

    def _source_key(self, dimension: str) -> str:
        facet = self.facets.get(dimension)
        return str(getattr(facet, "source_key", None) or dimension)

    def _on_attributes(self, dimension: str) -> bool:
        """Fuziunea citește preferința din `attributes` (`preference_level`). O dimensiune care nu
        e o fațetă de ATRIBUT (marca e o coloană pe catalogul real) n-ar ordona nimic, ci ar dilua
        celelalte preferințe (media pe dimensiuni), deci nu intră în `prefer`."""
        facet = self.facets.get(dimension)
        source = getattr(getattr(facet, "source", None), "value", None)
        return facet is not None and source == "attribute"

    def _catalog_value(self, source: str, value: str) -> str:
        """Valoarea din stare (normalizată: lower, fără diacritice) → cheia EXACTĂ din catalog,
        cum o poartă `attributes`. Fuziunea compară preferința cu atributul literal
        (`preference_level`), deci «samsung» n-ar fi potrivit „Samsung" și ar fi COBORÂT exact marca
        cerută. Fără vocabular, sau fără intrare, rămâne valoarea din stare (codurile canonice sunt
        deja așa)."""
        wanted = norm_text(value)
        for entry in self.vocab.entries(source) if self.vocab is not None else ():
            if norm_text(entry.key) == wanted:
                return entry.key
        return value

    # --- verdictul porții ------------------------------------------------------------------------

    def run(self) -> PlannedTurn:
        acts = list(enumerate(self.interp.acts))
        kept = [(i, a) for i, a in acts if i not in self.gate.skipped_acts]
        for _ in self.gate.skipped_acts:
            self._disclose(TURN_LEVEL, "invalid_target")
        if self.gate.decision.verdict == "must_ask":
            return self._finish((self._ask(kept),), dropped=0, primary=0)
        if not kept:
            return self._finish((self._plan("reply_only"),), dropped=0, primary=0)
        selected, dropped = self._select(kept)
        plans: list[TurnPlan] = []
        for index, (_, act) in enumerate(selected):
            plan = self._plan_act(act, index)
            plans.append(
                plan.model_copy(update={"depends_on": self._depends(selected, index, plan)})
            )
        primary_act = kept[-1][0]
        primary = next(n for n, (i, _) in enumerate(selected) if i == primary_act)
        return self._finish(tuple(plans), dropped=dropped, primary=primary)

    def _finish(self, plans: tuple[TurnPlan, ...], *, dropped: int, primary: int) -> PlannedTurn:
        return PlannedTurn(
            plans=plans,
            dropped_acts=dropped,
            gaps=tuple(self.gaps),
            disclosures=tuple(self.disclosures),
            primary=primary,
        )

    def _ask(self, kept: list[tuple[int, Act]]) -> TurnPlan:
        """`must_ask`: un singur plan. Candidații intră în plan doar când întrebarea e chiar pe o
        țintă a turului (cheia porții = cheia candidaților ei); o întrebare pusă pe ecran nu aduce
        id-uri în plan (I1: doar din `resolved`)."""
        if not self.gate.decision.question:
            self._gap("no_question")
            return self._plan("reply_only")
        ids: list[str] = []
        for _, act in kept:
            for ref in self._refs_of(act):
                if ref.outcome == "exact" or not ref.product_ids:
                    continue
                if target_question_key(ref.product_ids) == self.gate.asked_key:
                    ids += [p for p in ref.product_ids if p not in ids]
        return self._plan("ask", ids)

    def _select(self, kept: list[tuple[int, Act]]) -> tuple[list[tuple[int, Act]], int]:
        """Cel mult două acte: cel principal (ultimul) și, dintre celelalte, prima mutație, altfel
        primul act. Ordinea de execuție: mutația înaintea citirii, apoi ordinea clientului."""
        if len(kept) <= MAX_PLANS:
            chosen = list(kept)
        else:
            primary, rest = kept[-1], kept[:-1]
            second = next((ia for ia in rest if ia[1].kind in MUTATING_ACTS), rest[0])
            chosen = [second, primary]
            for _ in range(len(kept) - MAX_PLANS):
                self._disclose(TURN_LEVEL, "dropped_act")
        chosen.sort(key=lambda ia: (ia[1].kind not in MUTATING_ACTS, ia[0]))
        return chosen, len(kept) - len(chosen)

    def _depends(self, selected: list[tuple[int, Act]], index: int, plan: TurnPlan) -> int | None:
        """Un plan depinde de unul anterior când țintesc aceeași referință sau același produs, sau
        când o schimbare a turului e relativă la ținta planului anterior („o husă pentru EL")."""
        act = selected[index][1]
        own_ids = {p for ref in self._refs_of(act) for p in ref.product_ids}
        relative = {c.relative_to for c in self.interp.changes if c.relative_to}
        for earlier in range(index):
            prev = selected[earlier][1]
            if set(act.targets) & set(prev.targets):
                return earlier
            if own_ids & {p for ref in self._refs_of(prev) for p in ref.product_ids}:
                return earlier
            if plan.executor in _SEARCHING and relative & set(prev.targets):
                return earlier
        return None

    # --- tabelul act → executor ------------------------------------------------------------------

    @staticmethod
    def _plan(
        executor: Executor, ids: Sequence[str] = (), args: SearchArgs | None = None
    ) -> TurnPlan:
        return TurnPlan(executor=executor, product_ids=list(ids), search_args=args, depends_on=None)

    def _plan_act(self, act: Act, index: int) -> TurnPlan:
        kind = act.kind
        if kind in _FIXED:
            return self._plan(_FIXED[kind])
        if kind == "find":
            return self._find(act, index)
        if kind == "show_more":
            return self._show_more(act, index)
        if kind in _READ_ACTS:
            return self._read(act, index)
        if kind == "cart":
            ids = [
                p for ref in self._refs_of(act) if ref.outcome == "exact" for p in ref.product_ids
            ]
            if not ids:
                self._disclose(index, "no_target")
                return self._plan("reply_only")
            return self._plan("cart", list(dict.fromkeys(ids)))
        # `bundle`: executorul îl declară pachetul (SOLE: `routine_plan`); fără el, o căutare.
        if bundle_executor(self.state, pack=self.pack, vocab=self.vocab) is not None:
            return self._bundle(act)
        return self._search(act, index)

    def _bundle(self, act: Act) -> TurnPlan:
        """`bundle` (D3, `kernel.v2.1`): familia rutinei din subiect și pachet
        (`routine_family`), argumentele din STARE ca la căutare (nevoile dure, bugetul dur,
        preferințele; I2, I7) și ancora = prima țintă `exact` a actului. Fără familie declarată,
        planul rămâne `bundle` fără argumente, deci pe calea de azi."""
        family = routine_family(self.state, pack=self.pack, vocab=self.vocab)
        if family is None:
            return self._plan("bundle")
        ids = [p for ref in self._refs_of(act) if ref.outcome == "exact" for p in ref.product_ids]
        phrase = self._subject_label() or family
        # Rutina NU relaxează filtrele pe pași (`routine_tools`), deci o nevoie spusă, dar
        # ne-`enforce_ready`, rămâne ordonare aici (recenzia NX-352: «rutina de seară» ar fi exclus
        # produsele `am_pm` din fiecare pas).
        args = self._search_args(phrase, act, product_name=None, relaxable=False)
        if args.price_max is not None and not self._sum_asked_this_turn():
            # bugetul conversației (al unui produs, «o cremă sub 50») nu plafonează suma rutinei
            args = args.model_copy(update={"price_max": None})
            self._gap("routine_budget")
        if args.rank_terms or args.sort_mode != "relevance":
            self._gap("routine_field")
        return TurnPlan(
            executor="bundle",
            product_ids=list(dict.fromkeys(ids))[:1],
            search_args=args,
            depends_on=None,
            family=family,
        )

    def _sum_asked_this_turn(self) -> bool:
        """Clientul a spus o SUMĂ în turul ăsta (o limită de preț cu număr, nu relativă la un
        produs). Doar ea poate fi plafonul total al unei rutine (recenzia D3)."""
        return any(
            c.dimension == PRICE_DIMENSION and c.number is not None and c.relative_to is None
            for c in self.interp.changes
        )

    def _find(self, act: Act, index: int) -> TurnPlan:
        words = read_query(act, pack=self.pack, locale=self.locale).has_words
        nothing = not (words or self._facet_needs() or self._missing_name(act))
        if not self._subject() and nothing:
            # Poarta a întrebat deja (`must_ask`); dacă n-a întrebat (câștig mic, deja întrebat),
            # nu există ce căuta: răspunsul determinist, numărat (P6).
            self._gap("no_subject")
            return self._plan("reply_only")
        return self._search(act, index)

    def _show_more(self, act: Act, index: int) -> TurnPlan:
        if not self.changed and self.state.active_search:
            return self._plan("page")
        # O rafinare («mai arată-mi, dar sub 100»), sau o sesiune care nu mai există: o căutare din
        # stare. Cuvintele unui `show_more` nu sunt o cerere nouă, deci subiectul întâi.
        if self._subject():
            return self._search(act, index, subject_first=True)
        if read_query(act, pack=self.pack, locale=self.locale).has_words:
            return self._search(act, index)
        self._gap("no_subject")
        return self._plan("reply_only")

    def _read(self, act: Act, index: int) -> TurnPlan:
        """`detail` / `link` / `compare`. Un nume negăsit sau o țintă dispărută din catalog ⇒ o
        căutare pe nume, cu dezvăluirea că nu e potrivirea exactă. Altfel executorul actului, cu
        țintele `exact` (și cu toți candidații, pe `act_both`)."""
        if not act.targets:
            self._disclose(index, "no_target")
            return self._plan("reply_only")
        refs = self._refs_of(act)
        missing = [r for r in refs if _lost(r)]
        if missing:
            self._disclose(index, "not_exact_match")
            name = self._name(missing[0].ref_id)
            if name is not None:
                return self._search(act, index, name=name)
            if self._subject():
                return self._search(act, index, subject_first=True)
            return self._plan("reply_only")
        both = self.gate.decision.verdict == "act_both"
        ids: list[str] = []
        for ref in refs:
            if ref.outcome == "exact" or (both and ref.outcome == "ambiguous"):
                ids += [p for p in ref.product_ids if p not in ids]
        if not ids:
            self._disclose(index, "no_target")
            return self._plan("reply_only")
        return self._plan(act.kind, ids)  # type: ignore[arg-type]

    def _name(self, ref_id: str) -> str | None:
        ref = self.refs.get(ref_id)
        name = (ref.name or "").strip() if ref is not None else ""
        return name or None

    # --- `SearchArgs` -----------------------------------------------------------------------------

    def _search(
        self, act: Act, index: int, *, name: str | None = None, subject_first: bool = False
    ) -> TurnPlan:
        words = read_query(act, pack=self.pack, locale=self.locale).has_words
        product_name = name or self._missing_name(act)
        # Argumentele întâi: ele spun ce dimensiuni intră CHIAR în căutare (`carried`), deci ce
        # cuvinte ale cererii sunt deja purtate de un filtru sau de o preferință.
        before = list(self.gaps)
        carried: dict[str, str] = {}
        args = self._search_args(_PENDING, act, product_name=product_name, carried=carried)
        subject = self._subject_label()
        topic = self.state.topic
        shelf_label = self._shelf_label()
        text, whole = _read_act_query(
            act.query,
            words,
            self.interp.changes,
            self.checked,
            carried,
            subject if subject_kinds(topic) else None,
            shelf_label,
            {t for n in (topic.category_key, shelf_label) if n for t in tokens(n)},
            self.locale,
            flags=self.flags,
        )
        # NX-352: căutarea se COMPUNE din ce a validat kernelul (subiectul, nevoile filtrate,
        # termenii nemapați); cererea întreagă, fără cuvintele negate, e ultima rezervă.
        if name is not None:
            phrase = name
        elif subject_first:
            # cu subiect, textul compus îl conține deja (plus cuvintele fără alt canal)
            phrase = (text if subject else None) or subject or text or whole or product_name
        else:
            phrase = text or whole or product_name
        if not phrase:
            self.gaps[:] = before  # fără căutare, golurile argumentelor nu spun nimic
            self._gap("no_query")
            return self._plan("reply_only")
        return self._plan("search", (), args.model_copy(update={"query": phrase}))

    def _missing_name(self, act: Act) -> str | None:
        """O referință `name` negăsită printre țintele actului: căutarea e plasa
        (`product_name`)."""
        for ref in self._refs_of(act):
            if ref.kind == "name" and ref.outcome == "not_found":
                return self._name(ref.ref_id)
        return None

    def _search_args(
        self,
        phrase: str,
        act: Act,
        *,
        product_name: str | None,
        carried: dict[str, str] | None = None,
        relaxable: bool = True,
    ) -> SearchArgs:
        """SINGURUL constructor de `SearchArgs` din kernel (I2). Totul vine din starea redusă și din
        semnalele turului; ce nu are câmp merge în `gaps`. `carried` (NX-352) primește, pe fiecare
        dimensiune care intră chiar în argumente, `filter` sau `prefer`. `relaxable=False` (rutina):
        o nevoie spusă, dar ne-`enforce_ready`, rămâne preferință, fiindcă rutina NU relaxează
        filtrele (recenzia NX-352)."""
        carried = {} if carried is None else carried
        price_max: float | None = None
        brand: str | None = None
        concerns: list[str] = []
        features: list[str] = []
        prefer: dict[str, list[str]] = {}
        rank: list[str] = []

        def _prefer(dimension: str, value: str) -> None:
            carried.setdefault(dimension, "prefer")
            source = self._source_key(dimension)
            bucket = prefer.setdefault(source, [])
            canonical = self._catalog_value(source, value)
            if canonical not in bucket:
                bucket.append(canonical)

        for need in self.state.active_needs():
            key, value = need.key, need.normalized_value
            dimension = self.needs.dimension_of(key)
            spec = self.needs.spec_for(key)
            if key == _BUDGET_MAX:
                if self._hard(need, PRICE_DIMENSION) and _usable_amount(value):
                    price_max = float(value)  # type: ignore[arg-type]
                    carried[PRICE_DIMENSION] = "filter"
                else:
                    self._gap("soft_budget")
            elif key == _BUDGET_MIN:
                self._gap("price_min")
            elif self.needs.bounds_for(dimension) is not None:
                self._gap("numeric_facet")
            elif spec is not None and spec.kind is NeedKind.EXCLUSION:
                self._gap("exclusion")
            elif key == UNMAPPED_KEY:
                if isinstance(value, str) and value and value not in rank:
                    rank.append(value)
            elif not isinstance(value, str) or not value:
                self._gap("unsupported_need")
            elif dimension == BRAND_DIMENSION:
                if self._hard(need, dimension):
                    brand = value
                    carried[dimension] = "filter"
                elif self._on_attributes(dimension):
                    _prefer(dimension, value)
                else:
                    self._gap("soft_brand")
            elif dimension in self.facets:
                # NX-352 (`kernel.v5.0`, decis de Adi pe 2026-09-29): o nevoie SPUSĂ de client
                # (`explicit`) e filtru RELAXABIL chiar pe o fațetă ne-`enforce_ready`: scara de
                # relaxare a căutării o scoate dacă nu iese nimic, ca pe v1. Doar descrisă
                # (`implicit`) rămâne ordonare. Sonda NX-351: ca preferință, «ai ceva anti aging?»
                # aducea o treime de produse anti-aging.
                spoken = relaxable and need.source == "user_explicit"
                if not self._hard(need, dimension) and not spoken:
                    if self._on_attributes(dimension):
                        _prefer(dimension, value)
                    else:
                        self._gap("unsupported_need")
                elif dimension in self.searchable or self._source_key(dimension) in self.searchable:
                    features.append(value)
                    carried[dimension] = "filter"
                else:
                    concerns.append(value)
                    carried[dimension] = "filter"
            else:
                self._gap("unsupported_need")

        for signal in self.ranking:
            value = signal.value
            if signal.relation == "avoid":
                # ÎNTÂI: un cuvânt ocolit nu are voie să devină termen care URCĂ produsele cu el.
                self._gap("exclusion")
            elif signal.dimension == UNMAPPED_KEY:
                if isinstance(value, str) and value and value not in rank:
                    rank.append(value)
            elif signal.dimension == PRICE_DIMENSION:
                self._gap("soft_budget")
            elif self.needs.bounds_for(signal.dimension) is not None:
                self._gap("numeric_facet")
            elif (
                signal.dimension in self.facets
                and self._on_attributes(signal.dimension)
                and isinstance(value, str)
                and value
            ):
                _prefer(signal.dimension, value)

        # NX-314 pe calea interpretată: tipul subiectului ORDONEAZĂ (fațeta nu e `enforce_ready`),
        # deci „cremă de față" pe un raft de 900 de produse urcă cremele, fără să scoată restul.
        kinds = subject_kinds(self.state.topic)
        if kinds and self._on_attributes(PRODUCT_TYPE):
            for kind in kinds:
                _prefer(PRODUCT_TYPE, kind)
        elif kinds:
            self._gap("subject_type")

        return SearchArgs(
            query=phrase,
            price_max=price_max,
            category=self.state.topic.category_key,
            brand=brand,
            concerns=list(dict.fromkeys(concerns)) or None,
            features=list(dict.fromkeys(features)) or None,
            sort_mode=self._sort_mode(act),
            product_name=product_name,
            rank_terms=rank[:MAX_UNMAPPED_PER_TOPIC],
            prefer=prefer,
        )

    def _sort_mode(self, act: Act) -> str:
        """O referință `extreme` pe preț sau rating ordonează căutarea; altfel `relevance`. Aceeași
        referință raportează golul unei variante negăsite (starea nu ține o variantă cerută)."""
        targeted = {t: self.refs[t] for t in act.targets if t in self.refs}
        mode = "relevance"
        for ref in (*targeted.values(), *self._untargeted()):
            if ref.kind == "attribute" and ref.dimension == VARIANT_DIMENSION:
                resolved = self.resolved.get(ref.id)
                if resolved is not None and resolved.outcome == "not_found":
                    self._gap("variant")
            if ref.kind != "extreme" or mode != "relevance":
                continue
            dimension = ref.dimension or PRICE_DIMENSION
            if dimension == PRICE_DIMENSION and ref.direction == "min":
                mode = "price_asc"
            elif dimension == PRICE_DIMENSION and ref.direction == "max":
                mode = "price_desc"
            elif dimension == RATING_DIMENSION and ref.direction != "min":
                mode = "rating_desc"
        return mode


def _usable_amount(value: object) -> bool:
    """Un plafon de preț cu care se poate filtra: număr finit, pozitiv, nu `bool`."""
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
        and value > 0
    )


def _lost(ref: ResolvedRef) -> bool:
    """Ținta a numit ceva ce catalogul nu mai are: un nume negăsit sau un produs dispărut."""
    if ref.outcome == "not_found":
        return ref.kind == "name"
    return ref.outcome == "stale" and ref.reason == "not_in_catalog"


def plan_turn(
    interp: TurnInterpretation,
    state: ConversationStateV2,
    ranking: Sequence[RankingSignal],
    resolved: Sequence[ResolvedRef],
    gate: GateOutcome,
    *,
    changed: bool,
    pack: object | None,
    vocab: CatalogVocabulary | None,
    locale: str,
    checked: Sequence[CheckedChange] | None = None,
) -> PlannedTurn:
    """PUR. Planul unui tur interpretat.

    `state` = starea DUPĂ reducer, cu ecranul de dinaintea turului (ce vede poarta); `ranking` =
    `TurnDelta.ranking`; `changed` = `TurnDelta.proposals` nevide, primit ca `bool` (nu recalculat
    din text), OBLIGATORIU (un apelant care l-ar uita ar pagina orice rafinare): pe `show_more`
    separă paginarea de o rafinare. `checked` (NX-352) = schimbările VALIDATE ale turului: doar
    cele acceptate pot scoate cuvinte din textul căutării (recenzia NX-352: o schimbare respinsă nu
    are voie să consume cererea). Fără ele, schimbările brute, tratate ca spuse (teste, sonde
    vechi). Nu ridică niciodată, iar o intrare fără nimic de făcut dă `reply_only` (P6)."""
    return _Planner(
        interp,
        state,
        ranking,
        resolved,
        gate,
        changed=changed,
        pack=pack,
        vocab=vocab,
        locale=locale,
        checked=checked,
    ).run()


def bundle_executor(
    state: ConversationStateV2, *, pack: object | None, vocab: CatalogVocabulary | None
) -> str | None:
    """Numele uneltei care servește `bundle` pe subiectul stării, sau None: fără subiect, sau fără
    intrare în `DomainPack.bundle_executors` pentru rădăcina raftului, raft sau `"*"`. PUR; pasul 6
    îl cheamă ca să afle CE unealtă execută planul `bundle`."""
    topic = state.topic
    if not topic.has_subject:
        return None
    table = getattr(pack, "bundle_executors", None)
    if not isinstance(table, Mapping) or not table:
        return None
    usable = vocab if vocab is not None and not vocab.is_empty() else None
    root = topic_root_of(usable, topic.category_key) if usable is not None else None
    for key in (root, topic.category_key, WILDCARD):
        if key and key in table:
            return str(table[key])
    return None


def routine_family(
    state: ConversationStateV2, *, pack: object | None, vocab: CatalogVocabulary | None
) -> str | None:
    """Familia rutinei pentru subiectul stării. PUR. Ordinea, de la cel mai precis la cel mai
    grosier (recenzia D3):

    1. TIPUL subiectului, prin `routine_steps.by_product_type` (tipul → `familie:pas`): o «cremă de
       față» e a rutinei de față chiar pe raftul `machiaj-fata` (NX-313: „Fata" e machiaj);
    2. cheia raftului în `family_by_shelf` (o intrare pe un subraft bate rădăcina lui);
    3. rădăcina raftului în `family_by_shelf`.

    `None` fără subiect sau fără nicio potrivire: planul `bundle` rămâne pe calea de azi."""
    spec = getattr(pack, "routine_steps", None)
    families = getattr(spec, "families", None) or {}
    by_type = getattr(spec, "by_product_type", None) or {}
    # NX-350: tipurile subiectului (`subject_kinds`: tipul spus clar, altfel umbrela; un tip DEDUS
    # nu bate umbrela). Mai multe decid familia doar când TOATE cele cunoscute sunt ale aceleiași.
    seen = {str(by_type[k]).partition(SEP)[0] for k in subject_kinds(state.topic) if k in by_type}
    if len(seen) == 1 and next(iter(seen)) in families:
        return next(iter(seen))
    key = state.topic.category_key
    table = getattr(spec, "family_by_shelf", None)
    if not key or not isinstance(table, Mapping) or not table:
        return None
    usable = vocab if vocab is not None and not vocab.is_empty() else None
    root = topic_root_of(usable, key) if usable is not None else None
    for shelf in (key, root):
        if shelf and shelf in table:
            return str(table[shelf])
    return None


def subject_kinds(topic: Topic) -> tuple[str, ...]:
    """NX-350: tipurile pe care le ordonează subiectul. Fără tip spus clar, UMBRELA (toate codurile
    cuvântului clientului); un tip DEDUS de cod nu o bate (recenzia NX-350, constatarea 7)."""
    stated = topic.product_type if not (topic.type_learned and topic.type_umbrella) else None
    return (stated,) if stated else topic.type_umbrella


__all__ = [
    "DELEGATE_TOOLS",
    "DISCLOSURES",
    "GAPS",
    "MAX_PLANS",
    "TURN_LEVEL",
    "routine_family",
    "PlannedTurn",
    "bundle_executor",
    "plan_turn",
]
