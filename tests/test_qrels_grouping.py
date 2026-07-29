"""NX-203 — gruparea pe două niveluri şi integritatea manifestului.

`family_id` şi `split_group_id` sunt EXPLICITE în date, nu derivate la runtime din text: o
schimbare de normalizare ar rescrie tăcut ponderarea unui benchmark deja rulat.
"""

import collections
import json
import pathlib

import pytest
from pydantic import ValidationError

from src.evals.retrieval.schema import Provenance, QrelJudgment, QrelsQuery, QrelsSet, Relevance

_ROOT = pathlib.Path(__file__).resolve().parents[1]
_MANIFEST = _ROOT / "tests/golden/qrels_manifest_v1.json"
_CONFIRMED = _ROOT / "tests/golden/qrels_confirmed.json"


def _manifest() -> dict:
    return json.loads(_MANIFEST.read_text(encoding="utf-8"))


def test_manifest_counts_match_entries_exactly():
    """`counts` a fost STALE: declara eligible=91 când erau 121, candidate=41 când erau 0 — 8 din
    17 dispoziţii greşite. Totalul ieşea 299 în ambele cazuri, deci o verificare pe sumă ar fi zis
    „e în regulă". De aceea se compară pe fiecare cheie, nu pe total."""
    man = _manifest()
    real = dict(collections.Counter(e["disposition"] for e in man["entries"]))
    assert man["counts"] == real, {
        k: (man["counts"].get(k), real.get(k))
        for k in set(man["counts"]) | set(real)
        if man["counts"].get(k) != real.get(k)
    }
    assert sum(real.values()) == len(man["entries"])


def test_every_entry_has_both_grouping_levels():
    for e in _manifest()["entries"]:
        assert e.get("family_id"), e["text"][:60]
        assert e.get("split_group_id"), e["text"][:60]


def test_family_never_crosses_split_groups_in_manifest():
    groups: dict[str, set[str]] = collections.defaultdict(set)
    for e in _manifest()["entries"]:
        groups[e["family_id"]].add(e["split_group_id"])
    assert not {f: g for f, g in groups.items() if len(g) > 1}


def _q(**kw) -> QrelsQuery:
    base = {
        "id": "q-1",
        "query": "ser pentru ten gras",
        "provenance": Provenance.real_sanitized,
        "catalog_version": "v1",
        "judgments": [QrelJudgment(product_id="p-1", relevance=Relevance.ideal)],
    }
    return QrelsQuery(**{**base, **kw})


def test_schema_rejects_family_split_across_groups():
    """Invariantul e structural, nu o convenţie. Dacă două variante ale aceluiaşi contract de
    adevăr ajung în felii diferite, holdout-ul e contaminat şi nicio agregare ulterioară nu repară
    asta — metrica ar arăta corect şi ar fi greşită."""
    with pytest.raises(ValidationError, match="familii împărţite"):
        QrelsSet(
            business_id="b",
            queries=[
                _q(id="q-1", family_id="fam-a", split_group_id="sg-1"),
                _q(id="q-2", query="ser ten gras", family_id="fam-a", split_group_id="sg-2"),
            ],
        )


def test_schema_accepts_distinct_families_in_one_split_group():
    """Invers e PERMIS şi necesar: „şampon păr uscat" şi „şampon păr uscat hidratant" au gold
    diferit (deci familii diferite), dar una ar antrena pe cealaltă — deci aceeaşi felie."""
    qset = QrelsSet(
        business_id="b",
        queries=[
            _q(id="q-1", family_id="fam-a", split_group_id="sg-1"),
            _q(id="q-2", query="ser ten gras usor", family_id="fam-b", split_group_id="sg-1"),
        ],
    )
    assert len({q.family_id for q in qset.queries}) == 2


def test_confirmed_qrels_paraphrases_declare_their_source():
    """O parafrază fără sursă declarată e o afirmaţie despre provenienţă pe care n-o poate verifica
    nimeni. Două intrări erau marcate `real_sanitized` deşi textul fusese rescris de mine."""
    d = json.loads(_CONFIRMED.read_text(encoding="utf-8"))
    manifest_texts = {e["text"] for e in _manifest()["entries"]}
    for q in d["queries"]:
        if q["provenance"] == "real_sanitized":
            assert q["query"] in manifest_texts, (
                f"marcat real_sanitized dar textul nu apare în trafic: {q['query']!r}"
            )
        if q["provenance"] == "paraphrase":
            assert q.get("derived_from"), f"parafrază fără sursă: {q['query']!r}"


def test_derived_queries_inherit_the_source_split_group():
    """Altfel o variantă poate ateriza în altă felie decât sursa ei — exact contaminarea pe care
    `split_group_id` există s-o prevină."""
    man = {e["text"]: e for e in _manifest()["entries"]}
    d = json.loads(_CONFIRMED.read_text(encoding="utf-8"))
    for q in d["queries"]:
        src = q.get("derived_from")
        if src and src in man:
            assert q["split_group_id"] == man[src]["split_group_id"], q["query"]


def test_confirmed_qrels_have_both_ids_on_every_query():
    """Niciun query fără ambele id-uri. Fără asta, tocmai intrările negrupate ar fi excluse tăcut
    din agregarea pe familie şi din protecţia contra contaminării."""
    d = json.loads(_CONFIRMED.read_text(encoding="utf-8"))
    missing = [
        q["id"] for q in d["queries"] if not q.get("family_id") or not q.get("split_group_id")
    ]
    assert not missing, missing
    # fără număr hardcodat: setul creşte la fiecare lot, iar un test care cere o valoare fixă ar
    # trebui editat de fiecare dată — devine zgomot, nu verificare.
    assert len(d["queries"]) >= 10


def test_partial_grouping_is_rejected_not_silently_skipped():
    """Verificarea de dinainte făcea `if q.family_id and q.split_group_id`, deci o intrare fără
    id-uri SĂREA controlul. O grupare parţială e mai rea decât niciuna: scuteşte exact ce n-a fost
    acoperit, iar setul pare valid."""
    with pytest.raises(ValidationError, match="grupare parţială"):
        QrelsSet(
            business_id="b",
            queries=[
                _q(id="q-1", family_id="fam-a", split_group_id="sg-1"),
                _q(id="q-2", query="alt ser"),  # fără niciun id
            ],
        )


def test_ungrouped_set_is_still_legitimate():
    """Un set complet negrupat rămâne valid — fixture-uri şi seturi vechi n-au de ce să pice."""
    assert QrelsSet(business_id="b", queries=[_q(id="q-1"), _q(id="q-2", query="alt ser")])


# --- agregare pe familie ----------------------------------------------------

from src.evals.retrieval.harness import RunConfig, run_benchmark  # noqa: E402


def _set(queries) -> QrelsSet:
    return QrelsSet(business_id="b", queries=queries)


def _run(qset, ranked_by_query):
    return run_benchmark(qset, lambda q: ranked_by_query[q], RunConfig(label="t"))


def test_format_only_duplicate_does_not_change_headline_weighting():
    """Testul central al agregării. Un query cules de două ori (diacritice/typo) NU trebuie să
    cântărească dublu: nu contează mai mult, doar a fost colectat de două ori.

    Verificat pe o metrică de RANKING şi pe rata de interzise, fiindcă se agregă diferit —
    prima prin medie, a doua prin OR."""
    base = [
        _q(id="a1", query="ser ten gras", family_id="fam-a", split_group_id="sg-a"),
        _q(
            id="b1",
            query="crema ten uscat",
            family_id="fam-b",
            split_group_id="sg-b",
            judgments=[QrelJudgment(product_id="p-2", relevance=Relevance.ideal)],
        ),
    ]
    ranked = {"ser ten gras": ["p-1"], "crema ten uscat": ["x"], "ser ten gras?": ["p-1"]}
    before = _run(_set(base), ranked)

    # aceeaşi întrebare, a doua oară, doar altă formă — ACEEAŞI familie
    dup = base + [_q(id="a2", query="ser ten gras?", family_id="fam-a", split_group_id="sg-a")]
    after = _run(_set(dup), ranked)

    assert before.n_queries == 2 and after.n_queries == 3
    assert before.n_families == after.n_families == 2  # ambele expuse, ca diferenţa să se vadă
    assert after.ndcg_at_6 == pytest.approx(before.ndcg_at_6)
    assert after.recall_at_20 == pytest.approx(before.recall_at_20)


def test_duplicate_in_its_own_family_would_have_skewed_the_score():
    """Contra-proba: dacă duplicatul primeşte familie PROPRIE, scorul se mută — exact distorsiunea
    pe care agregarea o elimină. Fără ea, testul de mai sus ar trece degeaba."""
    base = [
        _q(id="a1", query="ser ten gras", family_id="fam-a", split_group_id="sg-a"),
        _q(
            id="b1",
            query="crema ten uscat",
            family_id="fam-b",
            split_group_id="sg-b",
            judgments=[QrelJudgment(product_id="p-2", relevance=Relevance.ideal)],
        ),
    ]
    ranked = {"ser ten gras": ["p-1"], "crema ten uscat": ["x"], "ser ten gras?": ["p-1"]}
    before = _run(_set(base), ranked)
    skewed = _run(
        _set(
            base
            + [_q(id="a2", query="ser ten gras?", family_id="fam-DIFERIT", split_group_id="sg-a2")]
        ),
        ranked,
    )
    assert skewed.ndcg_at_6 != pytest.approx(before.ndcg_at_6)


def test_forbidden_rate_is_or_within_family():
    """O încălcare de constrângere nu se mediază, se numără: dacă o singură variantă scoate un
    produs interzis, familia e violată. Altfel, adăugarea unei variante „curate" ar dilua-o."""
    qs = [
        _q(
            id="a1",
            query="q curat",
            family_id="fam-a",
            split_group_id="sg-a",
            forbidden_products=["bad"],
        ),
        _q(
            id="a2",
            query="q murdar",
            family_id="fam-a",
            split_group_id="sg-a",
            forbidden_products=["bad"],
        ),
    ]
    # doar A DOUA variantă scoate produsul interzis
    rep = _run(_set(qs), {"q curat": ["p-1"], "q murdar": ["bad", "p-1"]})
    assert rep.n_families == 1
    assert rep.forbidden_violation_rate == 1.0, (
        "familia trebuie violată dacă ORICE variantă o violează"
    )


def test_legacy_ungrouped_set_treats_each_query_as_singleton_family():
    """Seturile vechi, complet negrupate, rămân valide: fiecare query devine familie singleton, deci
    agregarea coincide exact cu media pe query."""
    qs = [_q(id="a1", query="q1"), _q(id="a2", query="q2")]
    rep = _run(_set(qs), {"q1": ["p-1"], "q2": ["p-1"]})
    assert rep.n_queries == rep.n_families == 2


def test_comparison_rejects_different_family_counts():
    """Cu alt număr de familii, deltele compară ponderări diferite şi arată ca o schimbare de
    calitate care nu s-a întâmplat."""
    from src.evals.retrieval.harness import compare_reports

    a = _run(_set([_q(id="a1", query="q1", family_id="f1", split_group_id="s1")]), {"q1": ["p-1"]})
    b = _run(
        _set(
            [
                _q(id="a1", query="q1", family_id="f1", split_group_id="s1"),
                _q(id="a2", query="q2", family_id="f2", split_group_id="s2"),
            ]
        ),
        {"q1": ["p-1"], "q2": ["p-1"]},
    )
    with pytest.raises(ValueError, match="număr diferit"):
        compare_reports(a, b)


def test_split_group_never_crosses_tuning_and_holdout():
    """Cerinţa centrală a nivelului doi. Fără ea, o întrebare în tuning şi varianta ei cu typo în
    holdout contaminează holdout-ul — iar nicio verificare pe TEXT nu le prinde, fiindcă textele
    chiar diferă."""
    # id-uri alese ca să cadă în felii DIFERITE dacă atribuirea s-ar face pe id
    from src.evals.retrieval.splits import Split, assign_split, partition, split_key

    a = next(f"q-{n}" for n in range(5000) if assign_split(f"q-{n}") is Split.tuning)
    b = next(f"q-{n}" for n in range(5000) if assign_split(f"q-{n}") is Split.holdout_h2)
    assert assign_split(a) != assign_split(b)  # premisa testului

    qset = _set(
        [
            _q(id=a, query="sampon par uscat", family_id="fam-1", split_group_id="sg-comun"),
            _q(
                id=b,
                query="sampon par uscat hidratant",
                family_id="fam-2",
                split_group_id="sg-comun",
            ),  # familie DIFERITĂ (alt gold), acelaşi grup de felie
        ]
    )
    parts = partition(qset)
    slices = {s for s, items in parts.items() if items}
    assert len(slices) == 1, f"grupul a fost împărţit între {slices}"
    assert all(split_key(q) == "sg-comun" for items in parts.values() for q in items)


def test_confirmed_qrels_split_groups_are_intact():
    """Pe datele reale, nu doar pe fixture."""
    from src.evals.retrieval.splits import partition

    d = json.loads(_CONFIRMED.read_text(encoding="utf-8"))
    parts = partition(QrelsSet(**d))
    seen: dict[str, str] = {}
    for slice_name, items in parts.items():
        for q in items:
            prev = seen.setdefault(q.split_group_id, slice_name.value)
            assert prev == slice_name.value, f"{q.split_group_id} în {prev} şi {slice_name.value}"


def test_validator_split_sizes_match_the_partition_actually_executed():
    """Poarta trebuie să valideze împărţirea care SE EXECUTĂ, nu una imaginară.

    `_split_sizes` hash-uia `query.id`, în timp ce `partition()` foloseşte `split_group_id`. Pentru
    un grup cu id-uri divergente, validatorul raporta două felii acolo unde partiţionarea reală
    face una singură — deci putea semnala contaminare sau dimensiuni pentru altceva decât
    ce rulează.
    O poartă care verifică altceva decât ce se întâmplă e mai rea decât una lipsă: dă încredere."""
    from src.evals.retrieval.splits import Split, assign_split, partition
    from src.evals.retrieval.validation import _split_sizes

    a = next(f"q-{n}" for n in range(5000) if assign_split(f"q-{n}") is Split.tuning)
    b = next(f"q-{n}" for n in range(5000) if assign_split(f"q-{n}") is Split.holdout_h2)
    assert assign_split(a) != assign_split(b), "premisa: id-urile diverg dacă se hash-uiesc separat"

    qset = _set(
        [
            _q(id=a, query="sampon par uscat", family_id="fam-1", split_group_id="sg-comun"),
            _q(
                id=b,
                query="sampon par uscat hidratant",
                family_id="fam-2",
                split_group_id="sg-comun",
            ),
        ]
    )
    real = {s.value: len(items) for s, items in partition(qset).items() if items}
    assert _split_sizes(qset) == real == {"tuning": 2}


def test_contamination_check_uses_the_same_key_as_partition():
    """Acelaşi text în două grupuri diferite -> contaminare raportată. Acelaşi text în ACELAŞI grup
    (doar cu id-uri divergente) -> nicio alarmă, fiindcă partiţionarea reală nu-l desparte."""
    from src.evals.retrieval.splits import Split, assign_split
    from src.evals.retrieval.validation import integrity_issues

    a = next(f"q-{n}" for n in range(5000) if assign_split(f"q-{n}") is Split.tuning)
    b = next(f"q-{n}" for n in range(5000) if assign_split(f"q-{n}") is Split.holdout_h2)

    same_group = _set(
        [
            _q(id=a, query="ser ten gras", family_id="f1", split_group_id="sg-x"),
            _q(id=b, query="Ser TEN gras ", family_id="f1", split_group_id="sg-x"),
        ]
    )
    assert not [i for i in integrity_issues(same_group) if "contaminat" in i]

    # grupurile trebuie ALESE ca să cadă în felii diferite — `sg-x`/`sg-y` sunt hash-uite la rândul
    # lor și pot nimeri în aceeași felie, caz în care nu există contaminare de raportat.
    g1 = next(f"sg-{n}" for n in range(5000) if assign_split(f"sg-{n}") is Split.tuning)
    g2 = next(f"sg-{n}" for n in range(5000) if assign_split(f"sg-{n}") is Split.holdout_h1)
    split_apart = _set(
        [
            _q(id=a, query="ser ten gras", family_id="f1", split_group_id=g1),
            _q(id=b, query="Ser TEN gras ", family_id="f2", split_group_id=g2),
        ]
    )
    assert [i for i in integrity_issues(split_apart) if "contaminat" in i]


def test_catalog_lookup_always_filters_active_and_published():
    """Orice numărare pe catalog trebuie să excludă produsele nepublicabile ÎNAINTE de concluzie.

    Fără asta am raportat „«vreau un parfum» → 4 produse reale, verificarea l-a salvat ca eligibil".
    Cele 4 erau `archived`+`draft`: retrieval-ul nu le poate returna niciodată. Nu e cazul în care
    n-am verificat — e cazul în care am verificat prost şi am tratat verificarea drept dovadă.

    Verificare pe TEXTUL scriptului, nu pe execuţie: scriptul rulează `main()` la import."""
    src = (_ROOT / "scripts/nx203_propose_qrels_candidates.py").read_text(encoding="utf-8")
    for pred in ("p.status = 'active'", "p.content_status = 'published'"):
        assert pred in src, f"filtrul de catalog nu mai conţine {pred!r}"
