"""Extragerea tipului de produs din numele de catalog (`src/catalog/product_type.py`).

Cazurile sunt luate din catalogul REAL SOLE, nu inventate: fiecare nume de mai jos există în
`products`. Motivul e cel din NX-268 — regula produce un atribut care ajunge într-un filtru DUR,
iar un atribut greșit nu mai e prins de nimeni în aval (validatorul stagiului 8 și
`grounding_guard` sunt porți de ADEVĂR, nu de potrivire).
"""

from __future__ import annotations

import pytest

from src.catalog.product_type import build_vocabulary, classify, raw_type, split_name


class TestSplitName:
    def test_capul_numelui_e_partea_dinaintea_descrierii(self) -> None:
        head, tail = split_name(
            "EVY TECHNOLOGY Sunscreen Mousse SPF 30, 150 ml - Crema de fata si corp "
            "formulata cu glicerina si filtre solare UVA UVB, care contribuie la protectia solara"
        )
        assert head == "EVY TECHNOLOGY Sunscreen Mousse SPF 30, 150 ml"
        assert tail and tail.startswith("Crema de fata")

    def test_un_nume_fara_coada_ramane_intreg(self) -> None:
        assert split_name("GESKE MicroCurrent Face-Lifter 6 in 1") == (
            "GESKE MicroCurrent Face-Lifter 6 in 1",
            None,
        )


class TestRawType:
    @pytest.mark.parametrize(
        ("name", "expected"),
        [
            (
                "ANUA Peach 77 Niacin Enriched Cream - crema de fata formulata cu niacinamida "
                "si pantenol, care contribuie la hidratarea pielii - 50 ml",
                "crema de fata",
            ),
            (
                "JUMISO Waterfull Hyaluronic Toner Renew - toner de fata formulat cu Acid "
                "Hialuronic si Ceramide - 250 ml",
                "toner de fata",
            ),
            (
                "EUBOS Urea Intensive Care 5% Urea Washing Lotion - gel de dus formulat cu uree",
                "gel de dus",
            ),
            # Virgula închide tipul: după ea vin volumul și nuanța.
            (
                "FWEE Lip and Cheek Blurry Pudding Pot - nuantator pentru buze formulat cu unt, "
                "5 gr - MV02 Hurt",
                "nuantator pentru buze",
            ),
            # Diacriticele se pliază exact ca `ro_unaccent` (033).
            ("BRAND X - cremă de față formulată cu ceva", "crema de fata"),
        ],
    )
    def test_tipul_iese_din_coada(self, name: str, expected: str) -> None:
        assert raw_type(name) == expected

    def test_fara_coada_nu_exista_tip(self) -> None:
        assert raw_type("MIZON Snail Repair Intensive Gold Benzi Hidrogel pentru Ochi") is None

    @pytest.mark.parametrize(
        "name",
        [
            # R2: o listă de beneficii nu e un obiect. Numele astea există în catalog și
            # produceau chei ca „hidratare si luminozitate" (16 produse) sau „calmare si".
            "TIRTIR Ceramic Cream - Hidratare Profunda si Calmare, 50 ml",
            "Mizon Phyto Plump Collagen Crema de Noapte - Hidratare Intensa si Fermitate",
            "BRAND - luminozitate si hidratare intensa",
        ],
    )
    def test_coordonarea_descalifica_un_tip(self, name: str) -> None:
        assert raw_type(name) is None

    @pytest.mark.parametrize("name", ["BRAND - de fata", "BRAND - luminozitate si", "BRAND - cu"])
    def test_o_fraza_taiata_nu_e_un_obiect(self, name: str) -> None:
        assert raw_type(name) is None

    def test_o_propozitie_intreaga_nu_e_un_tip(self) -> None:
        assert raw_type("BRAND - unul dintre cele mai bune produse de pe piata romaneasca") is None


class TestVocabulary:
    def test_calificativul_alipit_se_pliaza(self) -> None:
        """R1: „cushion" e format, nu clasă. Un fond de ten cushion rămâne fond de ten."""
        names = [f"B{i} - fond de ten formulat cu x" for i in range(5)] + [
            f"C{i} - fond de ten cushion formulat cu x" for i in range(5)
        ]
        mapping, counts = build_vocabulary(names)
        assert mapping["fond de ten cushion"] == "fond de ten"
        assert counts == {"fond de ten": 10}

    def test_calificativul_prepozitional_NU_se_pliaza(self) -> None:
        """R1, direcția care contează: un balsam de buze nu e un balsam de păr.

        Regula de fuziune pe SUBSET (încercată prima) pliază ambele peste „balsam" și produce o
        cheie care amestecă două produse fără legătură — exact eroarea pe care fațeta
        `partitioning` trebuie s-o excludă.
        """
        names = (
            [f"A{i} - balsam formulat cu x" for i in range(5)]
            + [f"B{i} - balsam de buze formulat cu x" for i in range(5)]
            + [f"C{i} - balsam de par formulat cu x" for i in range(5)]
        )
        _, counts = build_vocabulary(names)
        assert counts == {"balsam": 5, "balsam de buze": 5, "balsam de par": 5}

    def test_sub_prag_valoarea_nu_intra_in_vocabular(self) -> None:
        names = [f"A{i} - crema de fata formulata cu x" for i in range(5)] + [
            "Z - aparat de microneedling formulat cu x"
        ]
        mapping, counts = build_vocabulary(names)
        assert counts == {"crema de fata": 5}
        assert "aparat de microneedling" not in mapping

    def test_rezultatul_nu_depinde_de_ordine(self) -> None:
        names = (
            [f"A{i} - crema de fata formulata cu x" for i in range(6)]
            + [f"B{i} - crema de fata hidratanta formulata cu x" for i in range(6)]
            + [f"C{i} - ser de fata formulat cu x" for i in range(6)]
        )
        first = build_vocabulary(names)
        assert build_vocabulary(list(reversed(names))) == first


class TestClassify:
    def test_un_tip_necunoscut_nu_devine_o_valoare(self) -> None:
        """UNKNOWN nu se convertește niciodată într-o valoare — aceeași regulă ca stocul de
        variantă (migrarea 047), din același motiv: „nu știm" și „nu e" nu sunt același lucru."""
        names = [f"A{i} - crema de fata formulata cu x" for i in range(5)]
        mapping, _ = build_vocabulary(names)
        assert classify("BRAND NOU - aparat de microneedling formulat cu x", mapping) is None
        assert classify("BRAND FARA COADA", mapping) is None
        assert classify("X - crema de fata formulata cu y", mapping) == "crema de fata"


# --- NX-271: auditul de precizie trebuie să măsoare PRODUCĂTORUL, nu o copie a lui ------------


def test_audit_derives_product_type_with_the_same_producer_as_the_job():
    """Auditul (`scripts/derived_precision_audit.py`) re-derivă valorile în loc să citească
    `attributes`, ca să măsoare regula, nu o scriere veche. Dar atunci el TREBUIE să cheme exact
    producătorul pe care îl cheamă jobul care scrie în catalog — altfel auditul dă un verdict
    despre alt sistem decât cel care rulează, iar `enforce_ready` s-ar aprinde pe o măsurătoare
    care nu descrie nimic.

    Garda e ieftină și structurală: ambele module trebuie să lege ACELEAȘI simboluri din
    `src.catalog.product_type`. O reimplementare locală în oricare dintre ele sparge testul."""
    import importlib.util
    import pathlib

    from src.catalog import product_type as canonical

    mods = {}
    for name in ("derived_precision_audit", "derive_product_type"):
        path = pathlib.Path(__file__).resolve().parents[1] / "scripts" / f"{name}.py"
        spec = importlib.util.spec_from_file_location(name, path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        mods[name] = mod

    for name, mod in mods.items():
        for symbol in ("build_vocabulary", "classify"):
            assert getattr(mod, symbol) is getattr(canonical, symbol), (
                f"{name} nu folosește `{symbol}` din src/catalog/product_type.py"
            )
        assert mod.MIN_SUPPORT is canonical.MIN_SUPPORT


def test_product_type_threshold_is_preregistered_above_the_claim_tier():
    """Pragul lui `product_type` nu poate coborî sub tierul de `claim`. Fațeta PARTIȚIONEAZĂ: sub
    enforcement decide cine intră în răspuns, iar o valoare greșită ascunde produsul corect —
    tăcut, și fără ca vreo poartă din aval s-o prindă (validatorul verifică adevărul, nu
    potrivirea). Testul apără disciplina de preînregistrare: pragul se schimbă doar deliberat."""
    import json
    import pathlib

    policy = json.loads(
        (
            pathlib.Path(__file__).resolve().parents[1] / "tests" / "derived_precision_policy.json"
        ).read_text(encoding="utf-8")
    )
    spec = policy["facets"]["product_type"]
    claim_tier = max(
        f["min_precision"] for f in policy["facets"].values() if f.get("promise") == "claim"
    )
    assert spec["promise"] == "partitioning"
    assert spec["min_precision"] >= claim_tier
    assert spec["min_sample"] >= 60
