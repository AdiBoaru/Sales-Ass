"""Gărzi pe uneltele de corpus NX-203.

Corpusul e INSTRUMENTUL DE MĂSURĂ: dacă el e subtil greșit, tot ce se măsoară cu el e greșit în
aceeași direcție și nimic din aval nu o poate detecta — un gate verde peste un motor stricat e mai
rău decât niciun gate. Testele de aici acoperă exact deciziile care s-au dovedit fragile în timpul
construcției, fiecare cu defectul real care le-a cerut.

Nu ating DB-ul: tot ce se testează e pur, iar partea cu DB e o citire, nu o decizie.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _load(name: str):
    """Scripturile din `scripts/` nu sunt un pachet importabil; le încărcăm după cale."""
    path = ROOT / "scripts" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


extract = _load("nx203_extract_families")
pools = _load("nx203_build_pools")


# ── parsarea frazelor ────────────────────────────────────────────────────────────────────────


def test_parse_phrases_ia_doar_ce_e_in_ghilimele():
    """Antetul secțiunii NU e o interogare. Fără regula asta, „Acest produs apare frecvent în
    recomandări" ar fi intrat în corpus ca familie."""
    body = (
        "Acest produs apare frecvent în recomandări atunci când cineva caută:\n"
        "• „deodorant roll-on natural pentru barbati”\n"
        "• „deodorant cu ulei de cedru organic”\n"
    )
    assert extract.parse_phrases(body) == [
        "deodorant roll-on natural pentru barbati",
        "deodorant cu ulei de cedru organic",
    ]


def test_parse_phrases_pe_corp_gol_sau_fara_ghilimele():
    assert extract.parse_phrases("") == []
    assert extract.parse_phrases("nicio frază citată aici") == []


# ── normalizarea: cele două capete ale unei potriviri ────────────────────────────────────────


def test_norm_words_si_ngrams_folosesc_aceeasi_transformare():
    """Defectul real: brandul „DR. KONOPKA'S" intra în index ca `dr. konopka's`, iar n-gramele
    frazei erau `dr konopka s` — potrivirea nu se producea NICIODATĂ, deci clasa `exact` ieșea
    goală. Cele două capete trebuie normalizate cu aceeași funcție."""
    brand = extract._norm_words("DR. KONOPKA'S")
    grams = extract._ngrams(extract.fold("DR. KONOPKA'S deodorant roll-on 50 ml"), 4)
    assert brand in grams


def test_ngrams_nu_potriveste_in_interiorul_unui_cuvant():
    """„ten" nu are voie să se potrivească în „intensiv": n-grama e delimitată de cuvinte."""
    grams = extract._ngrams("crema intensiva de fata", 2)
    assert "ten" not in grams
    assert "crema" in grams
    assert "crema intensiva" in grams


# ── cheia de familie = contractul de adevăr ──────────────────────────────────────────────────


def _phrase(**kw):
    base = {"text": "x", "folded": "x", "product_id": "p1", "product_name": "N"}
    return extract.Phrase(**{**base, **kw})


def test_familia_exacta_e_per_produs():
    """O cerere de produs anume nu se fuzionează cu raftul: altfel o potrivire exactă ar fi
    măsurată ca recomandare, adică alt contract."""
    a = _phrase(product_id="p1", klass="exact", product_type="crema de fata")
    b = _phrase(product_id="p2", klass="exact", product_type="crema de fata")
    assert a.family_key != b.family_key
    assert a.family_key.startswith("exact:")


def test_tipul_nu_intra_de_doua_ori_in_cheie():
    """`product_type` e și fațetă, și axa principală. Dacă ar apărea în ambele locuri, două chei
    identice semantic ar produce familii diferite după ordinea dicționarului."""
    ph = _phrase(
        product_type="ser de fata",
        facets=(("product_type", "ser de fata"), ("concerns", "riduri")),
    )
    assert ph.family_key.count("ser de fata") == 1


def test_reziduurile_despart_cereri_diferite():
    """Fără ele, „șampon cu cica" și „șampon cu salicilic" sunt aceeași familie, fiindcă părul
    n-are nicio fațetă în catalogul SOLE."""
    a = _phrase(product_type="sampon", residuals=("cica",))
    b = _phrase(product_type="sampon", residuals=("salicilic",))
    assert a.family_key != b.family_key


def test_family_id_e_stabil_intre_rulari():
    """Un id instabil ar rescrie manifestul la fiecare re-extragere și ar invalida etichete deja
    puse — costul e timp uman deja cheltuit."""
    assert extract._family_id("sampon||cica") == extract._family_id("sampon||cica")
    assert extract._family_id("sampon||cica") != extract._family_id("sampon||salicilic")


# ── negația: cel mai grav defect găsit la verificare ─────────────────────────────────────────


def test_negatia_separa_cereri_opuse():
    """Defectul real, găsit la spot check: „balsam pentru volum păr fin" și „balsam pentru par fin
    fara volum" cădeau în ACEEAȘI familie. Sunt cereri opuse — un motor care întoarce balsamuri de
    volum ar fi ieșit „corect" pe amândouă, iar corpusul n-ar fi putut măsura niciodată negația."""
    assert extract.negated_words("balsam pentru par fin fara volum") == {"volum"}
    assert extract.negated_words("balsam pentru volum par fin") == set()


def test_negatia_se_opreste_la_o_afirmatie_noua():
    """În „fara parabeni cu acid hialuronic", acidul e CERUT. O negație care se întinde până la
    capătul frazei ar interzice exact ce cere clientul."""
    negated = extract.negated_words("gel fara parabeni cu acid hialuronic")
    assert "parabeni" in negated
    assert "acid" not in negated and "hialuronic" not in negated


def test_negatia_acopera_o_enumerare_intreaga():
    """„si"/„sau" continuă enumerarea, nu o închid: oprirea la primul „si" ar lăsa al doilea termen
    ca o CERINȚĂ, adică inversat. Conectorii nu consumă din domeniul negației."""
    assert extract.negated_words("gel fara parabeni si sulfati si siliconi") == {
        "parabeni",
        "sulfati",
        "siliconi",
    }


def test_salienta_ignora_prefixul_de_negatie():
    """Defectul real, prins la verificare: `!volum` se compara cu vocabularul catalogului CU TOT CU
    prefix, deci pica mereu si fiecare reziduu negat disparea din cheie. Fixul de negatie era inert
    si nu avea niciun semn — zero familii afectate, nicio eroare."""
    catalog = {"volum"}
    assert extract.is_salient("volum", 10, catalog)
    assert extract.is_salient(extract.NEGATED_PREFIX + "volum", 10, catalog)
    assert not extract.is_salient("inexistent", 10, catalog)


def test_marcatorul_de_negatie_nu_e_un_termen():
    """„fara" ajungea in cheia familiei langa `!transfer` si o despartea de formulari identice."""
    ph = _phrase(text="fond de ten fara transfer", folded="fond de ten fara transfer")
    extract.classify_phrase(ph, {}, brands=set())
    assert "fara" not in ph.residual_pool
    assert extract.NEGATED_PREFIX + "transfer" in ph.residual_pool


def test_reziduul_negat_schimba_familia():
    """Separarea trebuie să fie STRUCTURALĂ (în cheie), nu o etichetă pusă alături."""
    pozitiv = _phrase(product_type="balsam", residuals=("volum",))
    negativ = _phrase(product_type="balsam", residuals=("!volum",))
    assert pozitiv.family_key != negativ.family_key


def test_valoarea_negata_nu_devine_cerinta():
    """O fațetă rostită sub negație e o INTERDICȚIE. Pusă în `hard_constraints`, benchmarkul ar
    număra drept încălcare tocmai răspunsul corect."""
    index = {"parfum": [("attributes", "parfum")]}
    ph = _phrase(
        text="crema fara parfum pentru ten sensibil", folded="crema fara parfum pentru ten sensibil"
    )
    extract.classify_phrase(ph, index, brands=set())
    assert ("attributes", "parfum") not in ph.facets
    assert ("attributes", "parfum") in ph.forbidden


def test_valoarea_neneagata_ramane_cerinta():
    index = {"parfum": [("attributes", "parfum")]}
    ph = _phrase(text="crema cu parfum de trandafir", folded="crema cu parfum de trandafir")
    extract.classify_phrase(ph, index, brands=set())
    assert ("attributes", "parfum") in ph.facets
    assert ph.forbidden == ()


# ── detectorul de fuziune prea largă ─────────────────────────────────────────────────────────


def test_suprapunerea_ignora_cuvintele_comune_prin_constructie():
    """Defectul real: măsurată pe TOATE cuvintele, suprapunerea ieșea mare fiindcă ambele fraze
    conțin „sampon", deci detectorul a găsit 3 familii suspecte din 779. Excluzând cuvintele care
    definesc familia, rămâne exact partea în care cererile diferă."""
    phrases = ["sampon cu cica si musetel", "sampon cu acid salicilic"]
    naiv = extract._phrase_overlap(phrases, set())
    informat = extract._phrase_overlap(phrases, {"sampon"})
    assert informat < naiv
    assert informat < extract._MIN_MERGE_OVERLAP


def test_suprapunerea_ramane_mare_pentru_parafraze_adevarate():
    """Simetric: două formulări ale ACELEIAȘI cereri nu trebuie marcate ca fuziune greșită."""
    phrases = ["crema pentru ten uscat", "crema pentru tenul uscat"]
    assert extract._phrase_overlap(phrases, {"crema"}) >= extract._MIN_MERGE_OVERLAP


# ── pool-ul de candidați ─────────────────────────────────────────────────────────────────────


def _fam(fid="f-1", merchant=()):
    return {"family_id": fid, "merchant_asserted_products": list(merchant)}


def test_pool_pastreaza_ambele_brate_cand_plafonul_taie():
    """Defectul real: varianta dinainte lua întâi TOATE afirmațiile comerciantului, deci o familie
    cu peste `cap` produse afirmate producea un pool fără niciun rezultat de motor — o familie pe
    care precizia motorului nu se poate măsura deloc."""
    merchant = [f"m{i}" for i in range(30)]
    engine = {f"e{i}": [f"raw#{i}"] for i in range(30)}
    pool = pools._assemble(_fam(merchant=merchant), engine, cap=10)
    ids = {e["product_id"] for e in pool}
    assert len(pool) == 10
    assert any(i.startswith("m") for i in ids), "brațul comerciantului a dispărut"
    assert any(i.startswith("e") for i in ids), "brațul motorului a dispărut"


def test_pool_nu_pierde_nimic_sub_plafon():
    pool = pools._assemble(_fam(merchant=["m1", "m2"]), {"e1": ["raw#0"]}, cap=10)
    assert {e["product_id"] for e in pool} == {"m1", "m2", "e1"}


def test_pool_pastreaza_sursele_unui_produs_gasit_de_ambele_brate():
    """Sursele spun mai târziu dacă un produs relevant a fost găsit de motor sau doar afirmat de
    comerciant — adică fix măsura punctului orb. Un `set` le-ar fi pierdut."""
    pool = pools._assemble(_fam(merchant=["p1"]), {"p1": ["raw#3", "constrained#0"]}, cap=10)
    sources = pool[0]["sources"]
    assert "merchant" in sources
    assert "raw#3" in sources and "constrained#0" in sources


def test_ordinea_pool_ului_nu_e_ordinea_motorului():
    """Dacă primele carduri ar fi mereu top-ul motorului, judecata umană s-ar ancora pe el și am
    eticheta motorul, nu marfa. Ordinea trebuie să fie deterministă, dar necorelată cu rangul."""
    engine = {f"e{i}": [f"raw#{i}"] for i in range(20)}
    pool = pools._assemble(_fam(), engine, cap=20)
    order = [e["product_id"] for e in pool]
    engine_order = [f"e{i}" for i in range(20)]
    assert order != engine_order
    assert pools._assemble(_fam(), engine, cap=20) == pool, "ordinea trebuie să fie reproductibilă"


def test_acelasi_produs_cade_pe_pozitii_diferite_in_familii_diferite():
    """Sămânța include `family_id` tocmai ca ordinea să nu fie o constantă globală pe care un
    evaluator o învață după câteva familii."""
    engine = {f"e{i}": ["raw#0"] for i in range(20)}
    a = [e["product_id"] for e in pools._assemble(_fam("f-a"), engine, cap=20)]
    b = [e["product_id"] for e in pools._assemble(_fam("f-b"), engine, cap=20)]
    assert a != b


def test_stratificarea_acopera_tipuri_nu_doar_cele_mai_bogate():
    """Tăiat de sus, setul ar fi numai șampon: familiile cu multe formulări se îngrămădesc pe
    câteva rafturi, iar un benchmark care nu acoperă un tip nu poate detecta o regresie pe el."""
    families = [
        {"family_id": f"s{i}", "product_type": "sampon", "queries": ["q"] * (100 - i)}
        for i in range(20)
    ] + [
        {"family_id": "t1", "product_type": "pasta de dinti", "queries": ["q"]},
        {"family_id": "r1", "product_type": "ruj", "queries": ["q"]},
    ]
    picked = pools._stratify(families, 6)
    types = {f["product_type"] for f in picked}
    assert len(picked) == 6
    assert types == {"sampon", "pasta de dinti", "ruj"}


@pytest.mark.parametrize("cap", [1, 3, 7, 24])
def test_pool_respecta_plafonul_indiferent_de_brate(cap):
    merchant = [f"m{i}" for i in range(15)]
    engine = {f"e{i}": ["raw#0"] for i in range(15)}
    assert len(pools._assemble(_fam(merchant=merchant), engine, cap=cap)) == cap
