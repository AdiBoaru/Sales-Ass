"""Teste pentru meniul de clarificare (`src/catalog/clarify_menu.py`).

Fiecare test de aici ține un defect MĂSURAT pe catalogul real al tenantului SOLE sau pe traficul
lui real (`conversation_traces`, 2026-09-16), nu unul imaginat. Patru dintre ele fixează defecte
pe care doar rularea pe date le-a scos la iveală, după ce codul „arăta corect":

  • `test_shelves_are_the_biggest_not_the_alphabetically_first` — meniul de rafturi ieșea
    «Barbati, Copii, Corp, Dermato cosmetice», adică cele mai MICI patru, fiindcă intrările erau
    tăiate înainte de a fi sortate după dovadă;
  • `test_fallback_completes_instead_of_replacing` — singura sugestie grounded dintr-un set era
    ARUNCATĂ ca să fie înlocuită cu etichete seci;
  • `test_alias_prefers_the_facet_subject` — `oily` se oferea drept «luciu» (cel mai scurt alias),
    un simptom care pe un chip nu mai înseamnă nimic;
  • `test_catalog_miss_does_not_fire_on_real_followups` — detectorul naiv avea 25% precizie pe
    traficul real și ar fi răspuns „nu vindem așa ceva" la „Trimite-mi linkul la produs".

Pur: fără DB, fără rețea, fără credite.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from src.catalog.clarify_menu import (
    ClarifyMenu,
    build_menu,
    catalog_evidence,
    ground_suggestions,
    is_catalog_miss,
    menu_dimensions,
)
from src.catalog.vocabulary import CatalogVocabulary, VocabEntry

# --- fixtures: un catalog de cosmetice în miniatură, cu formele reale din SOLE ----------------


@dataclass(frozen=True)
class FakeFacet:
    key: str
    binding: str = "additive"
    aliases: dict[str, str] = field(default_factory=dict)
    labels: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class FakePack:
    facets: tuple[FakeFacet, ...] = ()
    concern_map: dict[str, str] = field(default_factory=dict)
    searchable_facets: tuple[str, ...] = ()


TEN = VocabEntry(key="ten", label="Ten", count=1461, path="ten")
MACHIAJ = VocabEntry(key="machiaj", label="Machiaj", count=681, path="machiaj")
PAR = VocabEntry(key="par", label="Par", count=233, path="par")
CORP = VocabEntry(key="corp", label="Corp", count=163, path="corp")
BARBATI = VocabEntry(key="barbati", label="Barbati", count=3, path="barbati")
INGRIJIRE_TEN = VocabEntry(
    key="ten-ingrijirea-tenului", label="Ingrijirea tenului", count=933, path="ten/ingrijirea"
)

CREMA = VocabEntry(key="crema de fata", label="crema de fata", count=287)
SER = VocabEntry(key="ser de fata", label="ser de fata", count=283)
MASCA = VocabEntry(key="masca de fata", label="masca de fata", count=239)
SAMPON = VocabEntry(key="sampon", label="sampon", count=64)

DRY = VocabEntry(key="dry", label="dry", count=429)
OILY = VocabEntry(key="oily", label="oily", count=318)

RIDURI = VocabEntry(key="anti_aging", label="anti_aging", count=666)
ACNEE = VocabEntry(key="acne", label="acne", count=518)

# Forme reale de chei TEHNICE din catalogul SOLE, care nu trebuie oferite niciodată.
PAS_RUTINA = VocabEntry(key="fata:tratament", label="fata:tratament", count=588)
NUANTA = VocabEntry(key="254d9a22fa84", label="254d9a22fa84", count=10)
VOLUM = VocabEntry(key="50 ml", label="50 ml", count=226)

PACK = FakePack(
    facets=(
        FakeFacet(key="category", binding="partitioning"),
        FakeFacet(key="product_type", binding="partitioning"),
        FakeFacet(key="skin_type", binding="partitioning", labels={"ro": "Tip de ten"}),
        FakeFacet(key="concerns", binding="additive", labels={"ro": "Potrivit pentru"}),
        FakeFacet(key="shade_group", binding="partitioning"),
        FakeFacet(key="volume_raw", binding="additive"),
    ),
    concern_map={
        # forme reale din pachetul SOLE, inclusiv ordinea „cel mai scurt alias" care păcălea
        "ten uscat": "dry",
        "uscaciune": "dry",
        "piele uscata": "dry",
        "luciu": "oily",
        "ten gras": "oily",
        "ma lucesc": "oily",
        "riduri": "anti_aging",
        "linii de expresie": "anti_aging",
        "acnee": "acne",
        # cheie DECLARATĂ de pachet dar ABSENTĂ din catalog (defectul `overlay_target_dead`)
        "ten mixt": "combination",
    },
    searchable_facets=("concerns",),
)


def _vocab(**dimensions: tuple[VocabEntry, ...]) -> CatalogVocabulary:
    return CatalogVocabulary(business_id="biz-1", dimensions=dict(dimensions))


FULL = _vocab(
    category=(BARBATI, CORP, MACHIAJ, PAR, TEN, INGRIJIRE_TEN),
    product_type=(CREMA, SER, MASCA, SAMPON),
    skin_type=(DRY, OILY),
    concerns=(RIDURI, ACNEE),
)
# Cheile de fațetă pe care le-ar întoarce `facet_keys_in_scope` pentru tot catalogul.
SCOPED_ALL = {
    "product_type": ["crema de fata", "ser de fata", "masca de fata", "sampon"],
    "skin_type": ["dry", "oily"],
    "concerns": ["anti_aging", "acne"],
}


# --- invarianta 1: round-trip ------------------------------------------------------------------


def test_every_option_resolves_back_to_a_key_with_products() -> None:
    """Invarianta care face meniul onest: orice frază oferită se întoarce, prin ACEEAȘI rezolvare
    pe care o face căutarea, pe o cheie cu produse. Dacă se rupe, botul promite rafturi goale."""
    from src.catalog.vocabulary import ResolutionStatus, facet_overlays, resolve_any

    menu = build_menu(FULL, PACK, locale="ro", scoped_keys=SCOPED_ALL)
    assert menu.options, "meniul trebuie să aibă opțiuni pe un catalog ne-gol"
    overlays = facet_overlays(PACK, FULL.facet_names)
    for option in menu.options:
        back = resolve_any(FULL, option.phrase, overlays=overlays)
        assert back.status is not ResolutionStatus.UNKNOWN, option.phrase
        assert back.evidence > 0, option.phrase


def test_dead_pack_key_is_never_offered() -> None:
    """«ten mixt» e declarat în pachet și NU există în catalog (`combination` are zero produse).
    Pe SOLE, exact patru fraze din 186 arată așa. Niciuna nu are voie să ajungă pe un chip."""
    menu = build_menu(
        FULL, PACK, locale="ro", scoped_keys={"skin_type": ["dry", "oily", "combination"]}
    )
    assert "ten mixt" not in menu.phrases()
    assert "combination" not in [o.key for o in menu.options]


def test_technical_keys_are_never_offered() -> None:
    """`fata:tratament`, `254d9a22fa84`, `50 ml` sunt vocabular CORECT pentru căutare și
    imposibil de rostit ca răspuns la „ce cauți?"."""
    vocab = _vocab(
        category=(TEN,),
        routine_step=(PAS_RUTINA,),
        shade_group=(NUANTA,),
        volume_raw=(VOLUM,),
    )
    pack = FakePack(
        facets=(
            FakeFacet(key="routine_step", binding="partitioning"),
            FakeFacet(key="shade_group", binding="partitioning"),
            FakeFacet(key="volume_raw"),
        )
    )
    menu = build_menu(
        vocab,
        pack,
        locale="ro",
        scoped_keys={
            "routine_step": ["fata:tratament"],
            "shade_group": ["254d9a22fa84"],
            "volume_raw": ["50 ml"],
        },
    )
    assert menu.phrases() == ("Ten",)


# --- defecte găsite la RULAREA pe catalogul real -----------------------------------------------


def test_shelves_are_the_biggest_not_the_alphabetically_first() -> None:
    """Rafturile se oferă în ordinea DOVEZII. Prima implementare tăia lista la patru intrări
    ÎNAINTE de a o sorta, iar vocabularul dă categoriile pe `path` (alfabetic): pe SOLE ieșeau
    «Barbati (3), Copii (5), Corp (163), Dermato cosmetice (6)» în loc de «Ten (1461), Machiaj
    (681), Par (233), Corp (163)». Meniul era adevărat și inutil."""
    menu = build_menu(FULL, PACK, locale="ro", catalog_miss=True)
    assert menu.phrases()[:4] == ("Ten", "Machiaj", "Par", "Corp")
    assert "Barbati" not in menu.phrases()


def test_alias_prefers_the_facet_subject() -> None:
    """`oily` are opt aliasuri pe SOLE, iar cel mai scurt e «luciu» — un simptom scos din context.
    Eticheta declarată a fațetei («Tip de ten») spune subiectul, deci câștigă «ten gras»."""
    menu = build_menu(FULL, PACK, locale="ro", scoped_keys={"skin_type": ["oily", "dry"]})
    assert "ten gras" in menu.phrases()
    assert "luciu" not in menu.phrases()
    # `concerns` se numește «Potrivit pentru»: niciun alias nu-l atinge, deci rămâne regula scurtă.
    menu2 = build_menu(FULL, PACK, locale="ro", scoped_keys={"concerns": ["anti_aging"]})
    assert "riduri" in menu2.phrases()
    assert "linii de expresie" not in menu2.phrases()


def test_fallback_completes_instead_of_replacing() -> None:
    """Dintr-un set real de patru sugestii, una singură era grounded. Varianta care ÎNLOCUIA
    lista la cădere o arunca tocmai pe aceea — pierdeam exact sugestia validată. Se completează."""
    menu = build_menu(FULL, PACK, locale="ro", scoped_keys=SCOPED_ALL)
    kept, dropped = ground_suggestions(
        [
            "Am tenul uscat si caut o crema de fata",  # conține «crema de fata» → trece
            "Un cablu USB-C de 2 metri",
        ],
        menu,
    )
    assert kept[0] == "Am tenul uscat si caut o crema de fata"
    assert len(kept) > 1, "lista se completează din meniu, nu rămâne cu una singură"
    assert "Un cablu USB-C de 2 metri" in dropped


# --- invarianta 3: fail-closed pe chip, fail-open pe vocabular ---------------------------------


def test_the_reported_bug_every_usb_chip_is_dropped() -> None:
    """Cazul care a produs cardul: patru sugestii despre cabluri USB pe un magazin de cosmetice.
    Niciuna nu numește ceva din catalog, deci niciuna nu supraviețuiește."""
    menu = build_menu(FULL, PACK, locale="ro", catalog_miss=True)
    kept, dropped = ground_suggestions(
        [
            "Pentru telefon, USB-C, 1-2 metri",
            "Pentru incarcator, USB-A la USB-C",
            "Pentru consola, cablu USB de date",
            "Nu stiu, imi recomanzi un cablu bun?",
        ],
        menu,
    )
    assert len(dropped) == 4
    assert all("USB" not in k and "cablu" not in k for k in kept)
    assert kept == ("Ten", "Machiaj", "Par", "Corp")


def test_model_phrasing_survives_diacritics_and_wrapping() -> None:
    """Meniul e închis, dar nu rigid: modelul are voie să îmbrace fraza natural, cu diacritice."""
    menu = build_menu(FULL, PACK, locale="ro", scoped_keys=SCOPED_ALL)
    kept, dropped = ground_suggestions(
        ["Caut o cremă de față pentru fiecare zi", "Am tenul uscat de la frig"], menu
    )
    assert "Caut o cremă de față pentru fiecare zi" in kept
    # «ten uscat» ≠ «tenul uscat» (fail-closed), iar «Ten» nu se potrivește ca SUBȘIR în „tenul":
    # potrivirea e pe cuvinte întregi, consecutive. Vezi `ground_suggestions`.
    assert dropped == ("Am tenul uscat de la frig",)


def test_empty_vocabulary_lets_everything_through() -> None:
    """DB jos ⇒ vocabular gol ⇒ totul iese UNKNOWN. O poartă care s-ar închide aici ar
    transforma o clipeală de DB în „botul nu mai sugerează nimic, niciodată" (P6)."""
    menu = build_menu(CatalogVocabulary(business_id="biz-1"), PACK, locale="ro")
    assert not menu.usable
    assert menu.reason == "vocabulary_unavailable"
    kept, dropped = ground_suggestions(["Pentru consola, cablu USB de date"], menu)
    assert kept == ("Pentru consola, cablu USB de date",)
    assert dropped == ()


def test_menu_object_without_options_is_transparent() -> None:
    """Aceeași poartă, exprimată pe contract: `usable=False` nu filtrează."""
    kept, dropped = ground_suggestions(["orice"], ClarifyMenu())
    assert kept == ("orice",)
    assert dropped == ()


# --- conversația: nu re-oferi ce clientul ți-a spus deja ---------------------------------------


def test_known_keys_are_not_offered_again() -> None:
    """Clientul a spus deja că are tenul uscat. A i-o re-oferi ca opțiune înseamnă să-l pui să
    repete — aceeași regulă ca poarta de clarificare NX-235, aplicată la nivel de opțiune."""
    menu = build_menu(
        FULL,
        PACK,
        locale="ro",
        known_keys=["dry"],
        scoped_keys={"skin_type": ["dry", "oily"]},
    )
    assert "ten uscat" not in menu.phrases()
    assert "ten gras" in menu.phrases()


def test_topic_restricts_categories_to_its_subtree() -> None:
    """Pe un raft discutat se oferă subarborele lui, nu tot magazinul: o conversație despre ten
    nu se închide cu «Machiaj»."""
    menu = build_menu(FULL, PACK, locale="ro", topic="ten")
    assert "Ingrijirea tenului" in menu.phrases()
    assert "Machiaj" not in menu.phrases()
    assert menu.reason == "topic"


def test_catalog_miss_offers_shelves_not_facets() -> None:
    """Unui client care a cerut un cablu nu-i pui întrebări despre tipul de ten: îi arăți din ce
    e făcut magazinul."""
    menu = build_menu(
        FULL, PACK, locale="ro", catalog_miss=True, scoped_keys={"skin_type": ["dry", "oily"]}
    )
    assert {o.dimension for o in menu.options} == {"category"}


# --- detectorul „catalogul n-are asta" ---------------------------------------------------------


def test_catalog_miss_fires_on_the_real_out_of_catalog_requests() -> None:
    """Cele două ture reale care au produs cardul."""
    for text in ("vreau un cablu usb", "Pentru telefon, USB-C, 1-2 metri"):
        assert is_catalog_miss(text, FULL, PACK, locale="ro", has_displayed_products=False), text


def test_catalog_miss_does_not_fire_on_real_followups() -> None:
    """Cele șase fals-pozitive MĂSURATE pe traficul real al tenantului. Regula naivă („≥2 termeni,
    niciunul rezolvabil") declanșa pe toate — 25% precizie — și ar fi răspuns „nu vindem așa ceva"
    unui client care cerea un link. Le opresc două condiții structurale, nu o listă de cuvinte:
    conversația arătase deja produse, sau un cuvânt e APROAPE de limba catalogului."""
    after_products = (
        "Compară-l cu un produs similar",
        "Trimite-mi linkul la produs",
        "de ce zici ca ma pune asta in evidenta ?",
        "care e diferenta dintre al 4lea si al 5lea ?",
    )
    for text in after_products:
        assert not is_catalog_miss(text, FULL, PACK, locale="ro", has_displayed_products=True), text

    typos = ("vreau o rutina pentru peiele sucata", "pai am zis piele")
    for text in typos:
        assert not is_catalog_miss(text, FULL, PACK, locale="ro", has_displayed_products=False), (
            text
        )


def test_catalog_miss_needs_more_than_one_unknown_word() -> None:
    """Cu un singur termen, absența dovezii e regula, nu excepția: «ieftin», «multumesc»."""
    for text in ("ieftin", "multumesc", "ok"):
        assert not is_catalog_miss(text, FULL, PACK, locale="ro", has_displayed_products=False), (
            text
        )


def test_catalog_miss_is_silent_without_a_vocabulary() -> None:
    """Fără vocabular nu știm nimic, deci n-avem dreptul la afirmația cea mai tare."""
    assert not is_catalog_miss(
        "vreau un cablu usb",
        CatalogVocabulary(business_id="biz-1"),
        PACK,
        locale="ro",
        has_displayed_products=False,
    )


def test_short_and_numeric_terms_are_not_evidence() -> None:
    """Pe catalogul SOLE, `c` se rezolvă pe «vitamina c» și `1`/`2` pe coduri de nuanță — de-asta
    un filtru „măcar un termen se rezolvă" ar fi PĂSTRAT chip-ul cu cablul USB-C de 1-2 metri."""
    vocab = _vocab(category=(TEN,), key_ingredients=(VocabEntry(key="c", label="c", count=150),))
    judged, hits = catalog_evidence("USB-C, 1-2 metri", vocab, locale="ro")
    assert "c" not in judged
    assert hits == ()


# --- ordinea axelor ----------------------------------------------------------------------------


def test_axis_order_prefers_declared_searchable_then_partitioning() -> None:
    """Ordinea vine din declarațiile pachetului, nu dintr-o preferință scrisă în cod. Prima
    variantă ordona doar pe `binding` + numărul de valori și scotea `concerns` («riduri»,
    «acnee») din meniu în favoarea lui `routine_time` («zi si noapte») — măsurat pe SOLE."""
    dims = menu_dimensions(FULL, PACK)
    assert dims[0] == "category"
    assert dims.index("concerns") < dims.index("skin_type")  # searchable bate partitioning
    assert dims.index("skin_type") < dims.index("product_type")  # apoi, mai puține valori întâi


def test_dimension_with_too_many_values_is_not_a_question() -> None:
    """`key_ingredients` are 200 de valori pe SOLE: excelentă la căutare, inutilă la oferit."""
    many = tuple(
        VocabEntry(key=f"ingredient {i}", label=f"ingredient {i}", count=100) for i in range(80)
    )
    vocab = _vocab(category=(TEN,), key_ingredients=many)
    pack = FakePack(
        facets=(FakeFacet(key="key_ingredients"),), searchable_facets=("key_ingredients",)
    )
    assert menu_dimensions(vocab, pack) == ("category",)


# --- firul real: stagiul de triaj chiar aplică poarta ------------------------------------------


class _FakeConn:
    """Un catalog minuscul servit prin ACELEAȘI interogări pe care le fac modulele reale.

    Dispecerizarea pe textul SQL e urâtă, dar e singura care dovedește ceva: un fake care
    întoarce direct un `CatalogVocabulary` ar sări exact peste `load_vocabulary`, adică peste
    bucata care poate să nu se potrivească cu ce scrie în DB."""

    async def fetch(self, sql: str, *args, **kwargs):
        if "from categories c" in sql and "count(distinct memb.product_id)" in sql:
            return [
                {"id": "1", "slug": "ten", "name": "Ten", "path": "ten", "n": 1461},
                {"id": "2", "slug": "machiaj", "name": "Machiaj", "path": "machiaj", "n": 681},
                {"id": "3", "slug": "par", "name": "Par", "path": "par", "n": 233},
            ]
        if "jsonb_each" in sql and "kv.key = any" in sql:  # facet_keys_in_scope
            return [
                {"dimension": "skin_type", "value": "dry", "n": 429},
                {"dimension": "skin_type", "value": "oily", "n": 318},
            ]
        if "jsonb_each" in sql:  # _ATTRIBUTE_SQL
            return [
                {"dimension": "skin_type", "value": "dry", "n": 429},
                {"dimension": "skin_type", "value": "oily", "n": 318},
            ]
        if "product_badges" in sql:
            return []
        if "c.n > 0" in sql:  # list_category_slugs
            return [{"slug": "ten"}, {"slug": "machiaj"}, {"slug": "par"}]
        return []


class _RecordingLLM:
    """Reține promptul primit, ca să putem verifica CE a văzut modelul."""

    def __init__(self, payload: dict) -> None:
        self.payload = payload
        self.user_prompt = ""

    async def classify_json(self, system: str, user: str, *, model: str | None = None) -> dict:
        self.user_prompt = user
        return self.payload


def _turn_ctx(body: str):
    from src.models import BusinessConfig, Contact, ConversationState, InboundMessage, TurnContext

    ctx = TurnContext(
        turn_id="t1",
        business=BusinessConfig(id="biz-clarify-menu", slug="sole-ro", name="SOLE"),
        contact=Contact(id="c1", business_id="biz-clarify-menu"),
        message=InboundMessage(provider_msg_id="m1", body=body),
        conversation_id="conv1",
        state=ConversationState(),
    )
    ctx.business.domain_pack = PACK
    ctx.language = "ro"
    return ctx


async def test_triage_stage_drops_ungrounded_chips_end_to_end(monkeypatch) -> None:
    """Firul întreg, cu mesajul REAL care a produs cardul: catalogul intră în prompt, iar
    sugestiile despre cabluri nu ajung la client."""
    from src.catalog.clarify_menu import clear_clarify_menu_cache
    from src.catalog.vocabulary_cache import clear_vocabulary_cache
    from src.worker.runner import PipelineDeps
    from src.worker.stages.triage import triage_stage

    clear_vocabulary_cache()
    clear_clarify_menu_cache()
    llm = _RecordingLLM(
        {
            "route": "clarify",
            "missing_field": "intent",
            "confidence": "low",
            "reply": "Ca sa te ajut mai bine, poti sa-mi spui mai exact ce cauti?",
            "suggestions": [
                "Pentru telefon, USB-C, 1-2 metri",
                "Pentru incarcator, USB-A la USB-C",
                "Pentru consola, cablu USB de date",
                "Nu stiu, imi recomanzi un cablu bun?",
            ],
        }
    )
    ctx = _turn_ctx("vreau un cablu usb")
    await triage_stage(ctx, PipelineDeps(conn=_FakeConn(), llm=llm))

    assert "Opțiuni reale din catalog" in llm.user_prompt
    assert "Ten" in llm.user_prompt
    assert "nu se regăsește în catalog" in llm.user_prompt  # a cerut ceva ce nu vindem
    chips = ctx.reply.suggestions
    assert all("USB" not in c and "cablu" not in c and "consola" not in c for c in chips), chips
    assert chips == ["Ten", "Machiaj", "Par"]


async def test_triage_stage_keeps_grounded_chips(monkeypatch) -> None:
    """Simetria care contează: o sugestie care numește catalogul NU se atinge."""
    from src.catalog.clarify_menu import clear_clarify_menu_cache
    from src.catalog.vocabulary_cache import clear_vocabulary_cache
    from src.worker.runner import PipelineDeps
    from src.worker.stages.triage import triage_stage

    clear_vocabulary_cache()
    clear_clarify_menu_cache()
    llm = _RecordingLLM(
        {
            "route": "clarify",
            "missing_field": "intent",
            "reply": "Pentru ce zona cauti?",
            "suggestions": ["Caut ceva pentru Ten", "Ma intereseaza Machiaj", "Ceva de pe Marte"],
        }
    )
    ctx = _turn_ctx("vreau ceva bun")
    await triage_stage(ctx, PipelineDeps(conn=_FakeConn(), llm=llm))

    chips = ctx.reply.suggestions
    assert "Caut ceva pentru Ten" in chips
    assert "Ma intereseaza Machiaj" in chips
    assert "Ceva de pe Marte" not in chips


async def test_triage_stage_is_unchanged_when_the_flag_is_off(monkeypatch) -> None:
    """Kill-switch: cu poarta stinsă, sugestiile modelului pleacă exact ca înainte."""
    from src.config import get_settings
    from src.worker.runner import PipelineDeps
    from src.worker.stages.triage import triage_stage

    get_settings.cache_clear()
    monkeypatch.setenv("CLARIFY_MENU_ENABLED", "false")
    try:
        llm = _RecordingLLM(
            {
                "route": "clarify",
                "missing_field": "intent",
                "reply": "Ce cauti?",
                "suggestions": ["Pentru consola, cablu USB de date"],
            }
        )
        ctx = _turn_ctx("vreau un cablu usb")
        await triage_stage(ctx, PipelineDeps(conn=_FakeConn(), llm=llm))
        assert ctx.reply.suggestions == ["Pentru consola, cablu USB de date"]
        assert "Opțiuni reale din catalog" not in llm.user_prompt
    finally:
        get_settings.cache_clear()
