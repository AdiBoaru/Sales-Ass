"""NX-273 — textele care ajung la model se compun din pachetul tenantului.

Poarta NX-264 a găsit 34 de scurgeri, toate în același loc conceptual: promptul agentului,
descrierile schemei de tool-uri, sugestiile de start, promptul de triaj, regexul de obligații. Nu
erau greșeli de neatenție — erau exemple puse ca să funcționeze bine pe clientul de azi, ceea ce le
face mai periculoase: fac sistemul mai bun pe SOLE și mai prost pe următorul, **fără semnal**.

Cazurile din card sunt marcate (happy 1-2, edge 3-5, failure 6-7)."""

from __future__ import annotations

import json
import pathlib

from src.agent.brain_models import _RECOMMEND_RE
from src.agent.prompt_builder import PromptInputs, build_agent_system
from src.agent.tool_definitions import tool_schemas
from src.domain import vocab_examples
from src.domain.facets import FacetSource, FacetType, TypedFacet
from src.domain.pack import DomainPack

ROOT = pathlib.Path(__file__).resolve().parent.parent
BASELINE = ROOT / "tests" / "domain_leak_baseline.json"

# Două pachete DIFERITE. Ăsta e tot testul: același cod, alte cuvinte, fără nicio ramură.
BEAUTY = DomainPack(
    vertical="ecommerce",
    concern_map={"ten gras": "oily", "acnee": "acne"},
    searchable_facets=("key_ingredients",),
    facets=(
        TypedFacet(
            key="key_ingredients",
            value_type=FacetType.LIST,
            source=FacetSource.ATTRIBUTE,
            source_key="key_ingredients",
            operators=("contains",),
            values=("niacinamida", "retinol"),
        ),
    ),
)
APPLIANCES = DomainPack(
    vertical="electrocasnice",
    concern_map={"consum mic": "eco", "zgomot redus": "silent"},
    searchable_facets=("clasa",),
    facets=(
        TypedFacet(
            key="clasa",
            value_type=FacetType.LIST,
            source=FacetSource.ATTRIBUTE,
            source_key="clasa",
            operators=("contains",),
            values=("A+++", "inverter"),
        ),
    ),
)


def _inputs(pack: DomainPack | None) -> PromptInputs:
    examples = vocab_examples.from_pack(pack)
    return PromptInputs.build(
        "Magazin",
        pack.vertical if pack else "ecommerce",
        "ro",
        ["o-categorie"],
        [],
        need_examples=examples.needs,
    )


# --- happy path ---------------------------------------------------------------------------------


def test_exemplele_din_prompt_sunt_cuvintele_tenantului():
    """Happy 1 — exemplele care ajung la model există în `concern_map`-ul lui, nu într-un fișier."""
    prompt = build_agent_system(_inputs(BEAUTY))
    assert "ten gras" in prompt and "acnee" in prompt

    other = build_agent_system(_inputs(APPLIANCES))
    assert "consum mic" in other and "zgomot redus" in other
    # și, mai important: vocabularul PRIMULUI nu se scurge în promptul celui de-al doilea
    assert "ten gras" not in other and "acnee" not in other


def test_doua_compuneri_dau_aceiasi_octeti():
    """Happy 2 — prefixul static byte-identic e ce dă reducerea de 75-90% la prompt caching. Un
    prompt compus dinamic o pierde dacă selecția sau ordinea variază de la tur la tur."""
    first = build_agent_system(_inputs(BEAUTY))
    second = build_agent_system(_inputs(BEAUTY))
    assert first == second
    # și instanțe construite separat trebuie să fie egale ca VALOARE (cheie de lru_cache)
    assert _inputs(BEAUTY) == _inputs(BEAUTY)


def test_schema_de_tooluri_urmeaza_acelasi_pachet():
    """Descrierea unui parametru nu e documentație, e o INSTRUCȚIUNE: un model care citește
    „ex. «ten gras»" învață ce fel de valori se așteaptă acolo."""

    def _desc(pack, param):
        schemas = tool_schemas(["search_products"], vocab_examples.from_pack(pack))
        return schemas[0]["function"]["parameters"]["properties"][param]["description"]

    assert "ten gras" in _desc(BEAUTY, "concerns")
    assert "niacinamida" in _desc(BEAUTY, "features")
    assert "consum mic" in _desc(APPLIANCES, "concerns")
    assert "ten gras" not in _desc(APPLIANCES, "concerns")


def test_schemele_nu_se_contamineaza_intre_tenanti():
    """`_SCHEMAS` e o constantă de modul partajată. O singură scriere în ea ar face ca al doilea
    tenant să primească exemplele primului — un bug de izolare care n-ar da nicio eroare."""
    tool_schemas(["search_products"], vocab_examples.from_pack(BEAUTY))
    neutral = tool_schemas(["search_products"])
    description = neutral[0]["function"]["parameters"]["properties"]["concerns"]["description"]
    assert "ten gras" not in description
    assert "{NEED_EXAMPLES}" not in description  # marcatorul nici el nu scapă spre model


def _concerns_clause(prompt: str) -> str:
    """Fraza care descrie `concerns` — singura pe care o umple pachetul. Se ia împreună cu linia
    următoare, fiindcă marcatorul de exemple cade la început de rând după înfășurare. Restul
    prompturilor au exemplele lor de FORMĂ (cum își numește clientul un produs), care nu sunt
    vocabular de vertical."""
    lines = prompt.splitlines()
    i = next(i for i, ln in enumerate(lines) if "cuvintele LUI" in ln)
    return " ".join(lines[i : i + 2])


# --- edge ---------------------------------------------------------------------------------------


def test_fara_concern_map_promptul_nu_are_exemple():
    """Edge 3 — un pachet fără nevoi produce un prompt FĂRĂ exemple, nu unul cu exemple de beauty.
    E diferența dintre a nu ști ce vinde clientul și a presupune greșit."""
    prompt = build_agent_system(_inputs(DomainPack(vertical="altceva")))
    # linia lui `concerns` — singura pe care o umple pachetul. Restul prompturilor au exemplele lor
    # de FORMĂ (cum își numește clientul un produs), care nu sunt vocabular de vertical.
    assert "ex. " not in _concerns_clause(prompt)
    assert "{NEED_EXAMPLES}" not in prompt  # marcatorul se înlocuiește, nu rămâne
    assert "search_products" in prompt  # promptul rămâne VALID, nu rupt
    # și, cu pachet, aceeași linie CHIAR primește exemple: altfel testul ar trece și pe un
    # prompt rupt
    assert "ex. " in _concerns_clause(build_agent_system(_inputs(BEAUTY)))


def test_pachet_cu_o_singura_nevoie_da_un_singur_exemplu():
    """Edge 4 — N exemple cerute, câte există. Nu se completează cu ce apucă."""
    pack = DomainPack(vertical="x", concern_map={"singura nevoie": "k"})
    examples = vocab_examples.from_pack(pack)
    assert examples.needs == ("singura nevoie",)


def test_selectia_e_ordinea_din_pachet_nu_alfabetica():
    """Edge — ordinea din pachet e o decizie a tenantului (primele intrări sunt cele
    reprezentative). Alfabetic ar alege arbitrar, dar tot determinist — deci testul verifică ce
    ALEGEM, nu doar că e stabil."""
    pack = DomainPack(vertical="x", concern_map={"zzz ultima": "a", "aaa prima": "b"})
    assert vocab_examples.from_pack(pack, limit=1).needs == ("zzz ultima",)


def test_clauza_goala_nu_lasa_paranteze_goale():
    """Un „(ex. )" în prompt arată a bug și îl învață pe model că lista e goală."""
    assert vocab_examples.clause(()) == ""
    assert vocab_examples.clause(("a",)) == " (ex. „a”)"


# --- failure ------------------------------------------------------------------------------------


def test_pachet_lipsa_sau_stricat_da_exemple_goale_nu_exceptie():
    """Failure 6 — fallback neutru, niciodată prompt gol sau crash (P6)."""
    assert vocab_examples.from_pack(None) is vocab_examples.EMPTY_EXAMPLES
    assert vocab_examples.from_pack(object()) == vocab_examples.EMPTY_EXAMPLES


def test_cricul_de_scurgeri_e_gol():
    """Failure 7 — dacă cineva readaugă un exemplu hardcodat, NX-264 pică, fiindcă intrarea de
    baseline a fost ȘTEARSĂ. Cricul gol e ce transformă reparația în una permanentă: testul cere ca
    o intrare care nu se mai potrivește să fie ștearsă, deci datoria poate doar să scadă."""
    data = json.loads(BASELINE.read_text(encoding="utf-8"))
    assert data["known"] == [], (
        "cricul NX-264 a primit intrări noi. Fiecare trebuie să vină cu motiv scris ȘI cu un card "
        f"care o repară: {data['known']}"
    )


# --- regexul de obligații ------------------------------------------------------------------------


def test_declansatorul_de_recomandare_e_verbul_nu_substantivul():
    """Aici era o listă de produse de cosmetice. Pe un magazin de electrocasnice regexul ar fi fost
    mort, fără ca nimic să pice — degradare tăcută, exact ce descrie cardul."""
    for message in ("ce crema imi recomanzi", "ce frigider aveti", "ce laptop as lua"):
        assert _RECOMMEND_RE.search(message), message
    assert not _RECOMMEND_RE.search("ce mai faci")
