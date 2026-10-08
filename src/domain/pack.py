"""DomainPack — contractul de configurare per-(business, vertical) (NX-114).

Mută politica/taxonomia per-vertical din COD în DB+seed (principiul 9): un vertical nou =
config (`src/domain/defaults/<vertical>.json` + override în `businesses.settings["domain_pack"]`),
NU deploy. `DomainPack` e PUR de date — nicio mapare hardcodată aici; loader-ul
(`src/domain/loader.py`) îl construiește din JSON-seed + override per-tenant, normalizat o
singură dată la încărcare (lookup-uri O(1) downstream).

Acesta e SKELETON-ul + contractul. Consumatorii hardcodați (taxonomy.py, gates.py RISK_PATTERNS,
greeting.py, profile.py) NU sunt migrați încă — wiring-ul lor e follow-up per-feature (NX-124 etc.).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from src.domain.constraints import EMPTY_UNITS, UnitRegistry
from src.domain.contracts import EMPTY_REQUIREMENTS, CategoryRequirements
from src.domain.facets import TypedFacet
from src.domain.relation_kinds import EMPTY_RELATION_KINDS, RelationKindRegistry
from src.domain.routine_steps import EMPTY_ROUTINE_STEPS, RoutineSpec

#: NX-329: dimensiunile care numesc produse pe orice vertical, când pachetul nu declară altele:
#: marca și tipul produsului. Restul (culoare, mărime, nuanță) le declară pachetul.
DEFAULT_REFERENCE_DIMENSIONS: tuple[str, ...] = ("brand", "product_type")

#: NX-336 PR C: codurile frazelor kernelului (`DomainPack.kernel_sentences`), vocabular ÎNCHIS.
#: Dezvăluirile plannerului (`turn_planner.DISCLOSURES`, verificat de test că sunt incluse) plus
#: răspunsul unei căutări fără rezultate (`no_results`) și închiderea unei comparații fără verdict
#: (`verdict_unknown`, cu eticheta dimensiunii, și `verdict_unknown_any`, fără ea: NX-336 C2,
#: I12), plus mutația coșului (D2): `cart_added`, `cart_failed` și refuzul porții fără întrebare
#: (`mutation_unavailable`, `mutation_not_exact`). Domeniul nu importă kernelul, deci lista se
#: repetă aici, iar testul ține cele două liste de acord. NX-372: răspunsul unui tur care a citit
#: doar regulile magazinului și n-a putut răspunde din ele, folosit și de calea v1
#: (`finalize.render`), fiindcă executorii `faq`/`delegate` compun prin ea. Patru fraze, după ce
#: s-a citit de fapt: `store_info_unknown` (citirea a mers și n-a adus nicio regulă, singurul caz
#: în care „nu am informația în reguli" e adevărat), `store_info_unconfirmed` (s-au citit reguli,
#: dar niciuna nu s-a putut servi ca răspuns, deci fraza nu afirmă că lipsește),
#: `store_info_unavailable` (toate citirile au picat) și `store_info_rest_unconfirmed` (după
#: regulile servite, pentru restul întrebării).
#: `need_unverifiable` (NX-374, `kernel.v6.1`): o nevoie spusă pe care catalogul nu o poate
#: verifica.
KERNEL_SENTENCE_CODES: frozenset[str] = frozenset(
    {
        "not_exact_match",
        "invalid_target",
        "dropped_act",
        "no_target",
        "no_results",
        "verdict_unknown",
        "verdict_unknown_any",
        "cart_added",
        "cart_failed",
        "mutation_unavailable",
        "mutation_not_exact",
        "store_info_unknown",
        "store_info_unconfirmed",
        "store_info_unavailable",
        "store_info_rest_unconfirmed",
        "need_unverifiable",
    }
)
#: Singurele coduri cu un marcator, fiecare exact o dată (loaderul respinge orice alt marcator):
#: `verdict_unknown` numește dimensiunea care lipsește, cu eticheta ei de rând din pachet, iar
#: `cart_added` produsul adăugat, cu numele lui scurt (NX-336 D2).
KERNEL_SENTENCE_MARKERS: dict[str, str] = {"verdict_unknown": "dimension", "cart_added": "product"}


def kernel_sentence(pack: object | None, locale: str | None, code: str) -> str | None:
    """Fraza unui cod din `kernel_sentences`, în limba turului (cu fallback pe limba de bază), sau
    `None`. PUR. Locuiește lângă date (NX-372): o citesc și kernelul, și calea v1 (`finalize`), iar
    o a doua copie a căutării ar putea alege altă limbă decât prima."""
    table = getattr(pack, "kernel_sentences", None) or {}
    lang = (locale or "").strip().lower()
    per_code = table.get(lang) or table.get(lang.split("-")[0]) or {}
    phrase = per_code.get(code)
    return phrase if isinstance(phrase, str) and phrase.strip() else None


def interpret_notes(pack: object | None, locale: str | None) -> tuple[str, ...]:
    """NX-380: notițele de magazin pentru interpretare, în limba turului (cu fallback pe limba de
    bază), sau `()`. PUR."""
    table = getattr(pack, "interpret_notes", None) or {}
    lang = (locale or "").strip().lower()
    notes = table.get(lang) or table.get(lang.split("-")[0]) or ()
    return tuple(n for n in notes if isinstance(n, str) and n.strip())


@dataclass(frozen=True)
class FacetSpec:
    """O fațetă de DOMENIU surfacing-uită în comparație (Tier 2, IZI-parity). GENERIC: `key` =
    cheia din `products.attributes` (ex. „concerns", „finish", „material", „spf"); `labels` =
    eticheta rândului per-locale; `value_labels` = traduceri OPȚIONALE cod→locale pentru valori
    canonice (ex. „oily"→„ten gras"). Valoare fără traducere → afișată ca atare (atribut deja
    display-ready). Zero hardcodat de vertical — totul din DomainPack (defaults JSON + override)."""

    key: str
    labels: dict[str, str] = field(default_factory=dict)  # locale → eticheta rândului
    value_labels: dict[str, dict[str, str]] = field(default_factory=dict)  # cod → locale → text
    #: NX-325: `False` ⇒ intrarea poartă DOAR etichete de valori (ex. `product_type`), fără rând
    #: în tabelul de comparație și fără loc în bundle-ul modelului. O singură listă în pachet, un
    #: singur proprietar al etichetelor; loaderul o împarte în cele două vederi.
    in_comparison: bool = True


@dataclass(frozen=True)
class SectionSpec:
    """O SECȚIUNE de fișă de produs (`product_sections.kind`) pe care vederea de detaliu a
    agentului o randează, cu plafonul ei de caractere. Ordinea în pachet = ordinea în vedere.

    De ce e config și nu cod: tipurile de secțiune sunt ale CONȚINUTULUI tenantului („cui i se
    potrivește", „când nu e alegerea potrivită", „pe scurt"), nu ale motorului. Măsurat pe primul
    catalog real: fișa avea 17 tipuri și codul randa unul singur, fiindcă lista era scrisă în cod
    pentru catalogul demo (docs/DB-QUERY-PROBE-2026-09-08.md)."""

    kind: str
    max_chars: int = 200


@dataclass(frozen=True)
class DomainPack:
    """Config per-(business, vertical). Owner: `load_domain_pack` (atașat pe BusinessConfig).
    Toate câmpurile au default-uri agnostice de vertical (P6 — un pack incomplet nu crapă)."""

    vertical: str  # verticalul tenantului (ecommerce | beauty_salon | auto_service | other | ...)
    # termen liber NORMALIZAT → cheia canonică din products.attributes->'concerns' (ex. "oily").
    concern_map: dict[str, str] = field(default_factory=dict)
    # NX-208: vocabular de EXPANDARE a interogării (query understanding). Frază colocvială
    # NORMALIZATĂ → termeni canonici de căutare adăugați la `search_text` (ex. „sa nu ma lucesc" →
    # ["matifiant", "mat"]). Separat de `concern_map` (acela mapează la o cheie de FILTRU pe
    # attributes->'concerns'; aici sunt termeni de TEXT care hrănesc lexical+semantic). Gol → fără
    # expandare (retrieval byte-identic). Per-vertical (defaults JSON) + override per-tenant (P9).
    query_expansions: dict[str, list[str]] = field(default_factory=dict)
    # locale → {reason: [phrase_norm,...]} — termeni de risc/legal keyed pe locale (P11).
    risk_terms: dict[str, dict[str, list[str]]] = field(default_factory=dict)
    # locale → saluturi adiționale NORMALIZATE (peste baza din greeting.py) (P11).
    greetings: dict[str, list[str]] = field(default_factory=dict)
    # NX-121: locale → pattern-uri NORMALIZATE de prompt-injection (peste baza neutră din cod, P9).
    # Ecranul e DETECTARE/observabilitate; apărarea reală = validatorul de stagiul 8.
    injection_patterns: dict[str, list[str]] = field(default_factory=dict)
    # chei permise în contacts.profile (peste minimul agnostic). NICIODATĂ PII (telefon/email/nume).
    profile_whitelist: frozenset[str] = frozenset()
    # NX-148: tipuri permise de conversation_facts per vertical. Extractorul aruncă tipurile
    # din afara listei (plasă anti-halucinație de memorie). NICIODATĂ PII (P12).
    fact_type_whitelist: frozenset[str] = frozenset()
    # statusuri „finalizat" pt check_order (ex. delivered/closed) — neutre pe vertical.
    settled_order_statuses: tuple[str, ...] = ()
    currency: str = "RON"  # moneda afișată (din businesses.settings["currency"], fallback RON)
    # IZI: praguri pt badge-ul DERIVAT de card (top_rating/top_reviews/deal_discount_pct). Gol →
    # default-uri agnostice de vertical din `src/worker/badges.py`. Override per-tenant în settings.
    badge_rules: dict[str, float] = field(default_factory=dict)
    # ARCH-2026 P0: ponderile scorului de ranking blended (relevance/rating/availability/sale/
    # concern). Gol → default-uri agnostice de vertical din `fusion.py` (`RANK_WEIGHTS`). Override
    # (parțial) per-tenant în settings → merge peste default-uri (un vertical „deal-driven" poate
    # urca `sale`, unul „premium" poate urca `rating`). Ne-hardcodat (P9).
    rank_weights: dict[str, float] = field(default_factory=dict)
    # Tier 2 (IZI-parity): fațete de DOMENIU pentru tabelul de comparație (rânduri finish/acoperire/
    # potrivit-pentru/material/..., din `products.attributes`). Ordinea = ordinea de afișare. Gol →
    # tabelul are doar rândurile generice (preț/rating/avantaje/brand) ca azi. Auto-scalează: când
    # `attributes` crește, rândurile apar fără schimbare de cod. Per-vertical (defaults JSON).
    comparison_facets: tuple[FacetSpec, ...] = ()
    #: NX-325: TOATE intrările din `comparison_facets` ale pachetului, inclusiv cele cu
    #: `in_comparison: false`. Citit doar de `value_label`; gol ⇒ `value_label` citește
    #: `comparison_facets` (pachete construite direct, în teste).
    facet_labels: tuple[FacetSpec, ...] = ()
    # Secțiunile de fișă pe care le vede modelul la detaliu, în ordine, cu plafon per secțiune.
    # Gol → lista istorică din cod (`_detail_view`), ca verticalele fără declarație să rămână
    # byte-identice. Per-vertical (defaults JSON) + override per-tenant.
    detail_sections: tuple[SectionSpec, ...] = ()
    # NX-315: care tipuri de secțiune (`product_sections.kind`) sunt INSTRUCȚIUNI DE FOLOSIRE, în
    # ordinea în care se citesc. Tot conținut al tenantului, deci config: pe SOLE sunt `usage` și
    # `dosage`, pe un magazin de electrocasnice ar fi „instalare". Gol → turul „cum se folosește"
    # nu primește instrucțiunile magazinului în compunere (comportamentul de dinainte).
    howto_sections: tuple[str, ...] = ()
    # Tier 2b p2: cheile din `attributes` (ARRAY) pe care le poate FILTRA search-ul de feature
    # („ceva cu niacinamidă" → key_ingredients). Match NORMALIZAT (lower + strip diacritice). Gol →
    # fără filtru de feature. Separat de concern_map (concerns are calea lor de mapare).
    searchable_facets: tuple[str, ...] = ()
    # NX-186: fațete TIPIZATE (tip/operatori/valori/missing_value/labels/prag coverage) — contractul
    # pt QuerySpec (NX-208) + Match Gate (NX-187). Aditiv peste `searchable_facets`. Sursa validată
    # contra allowlist-ului din cod (facets.py), fail-closed pe config invalid. Gol → fără fațete
    # tipizate (comportament de azi). Per-vertical (defaults JSON).
    facets: tuple[TypedFacet, ...] = ()
    # NX-159 felia 3: profilul de STIL per business (ton / nivel_detaliu / reguli_salut /
    # reguli_upsell / disclaimere) — directive scurte (RO), INPUT de model pe căile de compunere
    # ale agentului (proză/order/rich), peste regulile dure de grounding și siguranță.
    # Gol → fără ghid de stil (byte-identic). Per-vertical (defaults JSON) + override per-tenant.
    response_style: dict[str, str] = field(default_factory=dict)
    # Șabloanele chips-urilor (NX-296): `kind` → locale → frază cu `{slot}`. Copy-ul unui chip e
    # limbă, iar limba e configurație (P11) — un fallback românesc scris în cod ar face pilotul
    # `ro` să pară că merge și ar tăcea pe orice alt tenant. Fără șablon pentru o mutare, mutarea
    # nu se oferă deloc.
    chip_templates: dict[str, dict[str, str]] = field(default_factory=dict)
    # NX-316 felia 3: aceleași feluri, în vocea clientului (întrebări, ca la iZi), citite DOAR sub
    # `CHIP_MOVES_V2_ENABLED` și cu fallback pe `chip_templates` per fel. Cheie separată fiindcă
    # șabloanele sunt date de pachet, nu cod: rescrise pe loc, flagul OFF n-ar mai fi byte-identic.
    chip_templates_v2: dict[str, dict[str, str]] = field(default_factory=dict)
    # Șabloanele de FORMĂ (NX-299): `framing` (ce clase de produs sunt pe masă) + `list_glue`
    # (legătura dinaintea ultimului element al unei enumerări), fiecare per locale. Același motiv
    # ca la `chip_templates` — copy-ul e limbă, iar limba e configurație (P11). Diferă însă poarta:
    # aici lipsa e fail-OPEN (fără șablon, încadrarea rămâne a modelului), fiindcă un pachet
    # incomplet n-are voie să ȘTEARGĂ o frază pe care modelul a scris-o corect.
    answer_shape_templates: dict[str, dict[str, str]] = field(default_factory=dict)
    # NX-332 (kernel pasul 4a): frazele întrebărilor de clarificare, `locale` → `kind` → frază cu
    # exact un `{options}` (forma din contract, deci INVERSĂ față de `answer_shape_templates`).
    # `kind` = `reference` / `scope` / `value` (valorile lui `Ambiguity.about`) plus cheile de date
    # `subject`, `confirm`, `conflict`, `generic`; `bound_lte` / `bound_gte` sunt etichetele
    # limitelor din întrebarea de conflict, cu exact un `{value}`. Kernelul nu ține nicio frază:
    # completează doar `{options}` din etichete canonice. Loaderul aruncă per intrare un șablon cu
    # alt marcator (fail-closed pe șablon), iar poarta coboară determinist când lipsește (P6).
    clarify_templates: dict[str, dict[str, str]] = field(default_factory=dict)
    # NX-336 PR C: frazele kernelului, `locale` → cod → frază fără niciun marcator (forma
    # `clarify_templates`). Codurile sunt vocabular ÎNCHIS (`KERNEL_SENTENCE_CODES`): dezvăluirile
    # plannerului plus răspunsul unei căutări fără rezultate. Fail-open pe o dezvăluire (fără
    # frază, turul pleacă fără ea), fail-closed pe `no_results` (fără frază, kernelul nu servește
    # turul și răspunde calea v1). Kernelul nu ține nicio frază (P11).
    kernel_sentences: dict[str, dict[str, str]] = field(default_factory=dict)
    # NX-380: notițele de MAGAZIN pentru interpretarea turului, `locale` → reguli scurte, arătate
    # modelului sub `STORE NOTES`. Instrucțiunile adaptorului sunt generice (P11, poarta I14), deci
    # ce ține de catalogul unui tenant (un raft omograf, o valoare de meniu pe care clienții o spun
    # altfel) e dată, nu cod. Plafonate de loader; goale = promptul de dinainte, byte-identic.
    interpret_notes: dict[str, tuple[str, ...]] = field(default_factory=dict)
    # NX-205: câmpurile OBLIGATORII per categorie — contractul de completitudine al catalogului.
    # Frunza BATE rădăcina (override, NU cumul — vezi `CategoryRequirements.required_for`): o
    # categorie de ochi cere `key_benefit`, dar NU moștenește `finish`-ul rădăcinii `machiaj`.
    # Erau hardcodate în `scripts/audit_catalog_v2.py`
    # (`REQUIRED_V3_BY_SLUG`/`_BY_ROOT`); acum sunt config per-vertical (P9), iar auditul le CITEȘTE
    # de aici. Gol → nicio cerință (verticalele fără contract de conținut rămân ca azi).
    required_attributes: CategoryRequirements = EMPTY_REQUIREMENTS
    # NX-262: ce ÎNSEAMNĂ fiecare tip de muchie din `product_relations` și cât adânc are voie
    # cineva s-o urmeze. Verticalul își NUMEȘTE muchiile (`routine_next` la beauty, `requires` /
    # `compatible_with` la electrocasnice); codul definește doar comportamentele posibile
    # (`src/domain/relation_kinds.py`). Gol → fiecare tip e vecini-direcți, adică EXACT
    # comportamentul de azi: tăcerea nu acordă traversare.
    relation_kinds: RelationKindRegistry = EMPTY_RELATION_KINDS
    # NX-266: unitatea CANONICĂ a fiecărei fațete numerice + factorii de conversie („0,05 l" = 50
    # ml) + operatorul implicit când clientul dă un număr fără cuvânt de comparație. Stă în date,
    # nu în cod, din două motive: un factor scris în cod ar fi scurgere de domeniu (P9, poarta
    # NX-264), iar presupunerea că toți tenanții măsoară la fel e falsă chiar în interiorul unui
    # vertical. Gol → extracția de constrângeri numerice nu produce nimic (comportamentul de azi).
    units: UnitRegistry = EMPTY_UNITS
    # NX-280: pașii unei RUTINE — `familie:pas`, ordinea lor, ce tip de produs ocupă fiecare pas și
    # ce atribut promovează un produs într-un pas anume (`spf` → protecție). Stă în date din același
    # motiv ca `relation_kinds`: verticalul își numește pașii (curățare/tonifiere la cosmetice,
    # pașii de instalare la electrocasnice), iar codul definește doar ce E un pas. Gol → niciun
    # produs n-are pas, adică exact comportamentul de dinaintea NX-280.
    routine_steps: RoutineSpec = EMPTY_ROUTINE_STEPS
    # NX-329 (kernel pasul 2, I24): dimensiunile care NUMESC produse, deci pot fi ținta unei
    # referințe de tip atribut («Samsung-ul», «varianta neagră»): identitate și variantă, nu nevoi.
    # O nevoie („roșeață") nu e o țintă de act, e o schimbare de stare. Loaderul refuză aici o
    # fațetă `binding: additive`, deci o linie de config nu poate transforma o nevoie în țintă.
    # Etichetele de variantă sunt mereu de referință (sunt ale produsului prin construcție).
    reference_dimensions: tuple[str, ...] = DEFAULT_REFERENCE_DIMENSIONS
    # NX-333 (kernel pasul 4b, contractul runda 3, punctul 21): CE unealtă servește actul `bundle`
    # („fă-mi o rutină", „un set complet"). Rădăcina raftului (sau `"*"` pentru orice raft) → numele
    # uneltei. Semantica unui pachet e a verticalului (SOLE: `routine_plan`), deci stă în date, nu
    # în kernel. Loaderul păstrează doar nume din `TOOL_NAMES`; fără intrare, plannerul servește
    # `bundle` ca pe o căutare.
    bundle_executors: dict[str, str] = field(default_factory=dict)

    def value_label(self, facet_key: str, code: str, locale: str | None) -> str | None:
        """Eticheta localizată a unei VALORI de fațetă, sau `None` dacă pachetul n-o declară.

        UN proprietar: `FacetSpec.value_labels` din `comparison_facets` (același pe care îl citește
        `compose._facet_value_label` când randează tabelul de comparație). Meniul de nevoi (NX-322)
        și încadrarea serverului (NX-325) citesc de aici, ca să nu apară o a doua tabelă de
        etichete care să divergă tăcut de prima. Fallback pe `ro` e al pachetului, nu al codului:
        lipsa unei traduceri întoarce `None`, iar apelantul decide (de obicei, cheia)."""
        for spec in self.facet_labels or self.comparison_facets:
            if spec.key != facet_key:
                continue
            trans = spec.value_labels.get(code) or {}
            return trans.get(locale or "") or None
        return None
