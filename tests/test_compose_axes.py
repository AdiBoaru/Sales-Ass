"""NX-139 — motorul GENERIC de axe de decizie + cifrele de specificație grounded.

Funcții pure (zero LLM/DB). Dovada de GENERALITATE (cerința Adi): aceleași funcții, fixture-uri
din 3 verticale diferite (beauty / piese auto / bijuterii) — doar config-ul (FacetSpec) diferă.
"""

from src.domain.pack import FacetSpec
from src.worker.compose import decision_axes, scrub_education, spec_numbers

# --- fixture-uri pe 3 verticale (config diferit, cod identic) -----------------

SKIN = FacetSpec(
    key="skin_type",
    labels={"ro": "Tip de ten"},
    value_labels={"dry": {"ro": "uscat"}, "oily": {"ro": "gras"}, "sensitive": {"ro": "sensibil"}},
)
BEAUTY = [
    {
        "id": "b1",
        "name": "Crema SPF 30, 50 ml",
        "price": 47.0,
        "attributes": {"skin_type": ["dry"]},
    },
    {"id": "b2", "name": "Crema SPF 50", "price": 114.0, "attributes": {"skin_type": ["oily"]}},
    {
        "id": "b3",
        "name": "Stick SPF 30",
        "price": 196.0,
        "attributes": {"skin_type": ["sensitive"]},
    },
]

FITMENT = FacetSpec(key="fitment", labels={"ro": "Compatibilitate"})
MATERIAL = FacetSpec(key="material", labels={"ro": "Material"})
AUTO = [
    {"id": "a1", "name": "Placute frana fata", "price": 120.0, "attributes": {"fitment": "Golf 5"}},
    {"id": "a2", "name": "Placute frana", "price": 250.0, "attributes": {"fitment": "Passat B6"}},
]
JEWELRY = [
    {"id": "j1", "name": "Inel aur 14K", "price": 800.0, "attributes": {"material": "aur"}},
    {"id": "j2", "name": "Inel argint 925", "price": 150.0, "attributes": {"material": "argint"}},
]


# --- decision_axes ------------------------------------------------------------


def test_axes_beauty_skin_type_and_price():
    axes = decision_axes(BEAUTY, (SKIN,), "ro")
    assert any(a.startswith("Tip de ten: ") and "uscat" in a and "gras" in a for a in axes)
    assert any(a.startswith("Preț: de la 47 la 196 lei") for a in axes)  # spread 4x ≥ 1.5x


def test_axes_auto_fitment():
    axes = decision_axes(AUTO, (FITMENT,), "ro")
    assert any("Compatibilitate: " in a and "Golf 5" in a and "Passat B6" in a for a in axes)


def test_axes_jewelry_material():
    axes = decision_axes(JEWELRY, (MATERIAL,), "ro")
    assert any("Material: " in a and "aur" in a and "argint" in a for a in axes)


def test_axes_no_dispersion_is_not_an_axis():
    # toate produsele au ACEEAȘI valoare → fațeta nu ajută alegerea → nu e axă
    same = [{**p, "attributes": {"skin_type": ["dry"]}} for p in BEAUTY]
    axes = decision_axes(same, (SKIN,), "ro")
    assert not any("Tip de ten" in a for a in axes)


def test_axes_empty_cases():
    assert decision_axes([], (SKIN,), "ro") == []  # set gol
    assert decision_axes(BEAUTY[:1], (SKIN,), "ro") == []  # un singur produs
    # fără fațete + preț uniform → nicio axă
    flat = [{"id": "x", "name": "A", "price": 50.0}, {"id": "y", "name": "B", "price": 55.0}]
    assert decision_axes(flat, (), "ro") == []


def test_axes_capped_at_three():
    f2 = FacetSpec(key="finish", labels={"ro": "Finish"})
    f3 = FacetSpec(key="scop", labels={"ro": "Scop"})
    prods = [
        {
            "id": f"p{i}",
            "name": "P",
            "price": 10.0 * (i + 1) ** 2,
            "attributes": {"skin_type": [v], "finish": v, "scop": v},
        }
        for i, v in enumerate(["dry", "oily", "sensitive"])
    ]
    axes = decision_axes(prods, (SKIN, f2, f3), "ro")
    assert len(axes) <= 3


# --- spec_numbers + scrub_education (cifre grounded) ---------------------------


def test_spec_numbers_from_names_and_facets():
    nums = spec_numbers(BEAUTY, (SKIN,), "ro")
    assert {"30", "50"} <= nums  # din nume: SPF 30, 50 ml, SPF 50
    assert "47" not in nums  # prețul NU intră (nu e în nume/fațete)


def test_education_keeps_grounded_spec_digits():
    allowed = spec_numbers(BEAUTY, (), "ro")
    edu = "Pentru expunere intensă alege SPF 50. Formulele blânde sunt potrivite tenului sensibil."
    out = scrub_education(edu, stock_present=True, allowed_numbers=allowed)
    assert out is not None and "SPF 50" in out  # cifra grounded supraviețuiește (gap-ul iZi)


def test_education_drops_ungrounded_digits_keeps_rest():
    allowed = spec_numbers(BEAUTY, (), "ro")
    edu = "Alege SPF 100 pentru plajă. Textura lejeră se așază bine sub machiaj."
    out = scrub_education(edu, stock_present=True, allowed_numbers=allowed)
    assert out is not None and "SPF 100" not in out  # cifra NEgrounded pică...
    assert "Textura lejeră" in out  # ...dar propoziția sigură rămâne (granular)


def test_education_price_digits_still_drop():
    # prețul unui produs (47) NU e în spec_numbers → o propoziție cu el pică (anti-halucinație preț)
    allowed = spec_numbers(BEAUTY, (SKIN,), "ro")
    out = scrub_education("Costă doar 47 lei acum.", stock_present=True, allowed_numbers=allowed)
    assert out is None


def test_education_backcompat_empty_allowed():
    # fără allowed (default) → semantica veche: ORICE cifră ucide propoziția
    out = scrub_education("Alege SPF 30 mereu. Sfat fără cifre.", stock_present=True)
    assert out == "Sfat fără cifre."
