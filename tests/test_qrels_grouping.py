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
