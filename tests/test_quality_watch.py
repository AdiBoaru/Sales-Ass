"""NX-272 — cele cinci cifre care rulează singure, și cele două lucruri care le pot face inutile.

Un raport de calitate se strică în două feluri, amândouă tăcute:

1. **publică o cifră care pare rezultat** pe un eșantion din care nu se poate concluziona nimic.
   Precedentul e chiar în proiect: „92% acoperire pe Buze", corect și complet irelevant;
2. **cară conținut de conversație** într-un artefact comis în repo. `usage_daily` și analytics-ul
   n-au voie să vadă mesaje (P12); un raport de calitate nu e o excepție pentru că e „doar pentru
   noi" — trăiește în git, adică pentru totdeauna.

Testele sunt pe funcția pură de verdict și pe artefactul REAL din `reports/`, nu pe unul construit
aici: cel construit ar dovedi că testul e de acord cu el însuși.
"""

from __future__ import annotations

import json
import pathlib
import re

from scripts.quality_watch import MIN_SAMPLES, _verdict
from src.observability.slo import (
    VERDICT_FAIL,
    VERDICT_INSUFFICIENT,
    VERDICT_PASS,
    VERDICT_UNKNOWN,
)

ROOT = pathlib.Path(__file__).resolve().parents[1]
REPORTS = ROOT / "reports"
DOC = ROOT / "docs" / "QUALITY-WATCH.md"


def _artifacts() -> list[pathlib.Path]:
    return sorted(REPORTS.glob("quality-watch-*.json"))


def test_esantionul_mic_nu_poate_produce_PASS():
    """Sub prag, verdictul e `INSUFFICIENT` ORICÂT de bună ar fi valoarea. O cifră bună pe trei
    observații nu e o veste bună, e absența unei vești."""
    assert _verdict(0.0, 0.1, MIN_SAMPLES - 1) == VERDICT_INSUFFICIENT
    assert _verdict(0.0, 0.1, MIN_SAMPLES) == VERDICT_PASS


def test_datele_lipsa_bat_esantionul_mic():
    """Ordinea condițiilor e contractul: instrumentul stricat (`None`) se raportează ca atare chiar
    și când eșantionul ar fi suficient. Altfel un instrument mort ar arăta ca un eșantion subțire,
    și l-ai aștepta să se îngroașe la infinit."""
    assert _verdict(None, 0.1, 10_000) == VERDICT_UNKNOWN
    assert _verdict(None, 0.1, 0) == VERDICT_UNKNOWN


def test_pragul_se_aplica_in_directia_declarata():
    """`higher_is_worse` există fiindcă jumătate din cifre sunt rate de eșec (mai mult = mai rău) și
    jumătate rate de succes. Un singur sens ar fi raportat exact invers pe a doua jumătate."""
    assert _verdict(0.2, 0.1, MIN_SAMPLES) == VERDICT_FAIL
    assert _verdict(0.2, 0.1, MIN_SAMPLES, higher_is_worse=False) == VERDICT_PASS


def test_artefactul_nu_contine_continut_de_conversatie():
    """Raportul e comis în git. Are voie să poarte numere, nume de metrici și verdicte — nimic din
    ce a scris sau a primit un client, niciun identificator de contact.

    Poarta e STRUCTURALĂ, nu lexicală, și prima încercare a arătat de ce trebuie să fie: un filtru
    pe caractere permise pica pe propria noastră notă de operare („rulează `goldset_annotate.py`"),
    iar singura cale de a-l face să treacă era să-l lărgesc — adică să-l fac să tacă și pe textul de
    client. Un text de client nu se deosebește de al nostru după vocabular sau după punctuație.

    Se deosebește după UNDE STĂ. Artefactul are o formă închisă: stringurile pot apărea doar sub
    chei declarate, toate scrise de noi. O cheie nouă care poartă text pică testul, chiar dacă
    textul pare inofensiv — fiindcă atunci cineva a deschis un loc în care conținutul de conversație
    poate ajunge mâine."""
    allowed_keys = {
        "business_id",
        "from",
        "to",
        "metric",
        "verdict",
        "note",
        "_note",
        "label",
    }
    # Timestampurile ISO sunt legitime (fereastra raportului) și seamănă cu un număr de telefon
    # pentru orice regex naiv. Se exclud ÎNAINTE de poarta de telefon, nu prin lărgirea ei: o
    # poartă lărgită ca să tacă pe un fals pozitiv tace și pe adevăratele scurgeri.
    iso = re.compile(r"^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}")
    for path in _artifacts():
        payload = json.loads(path.read_text(encoding="utf-8"))
        for key, value in _strings(payload):
            assert key in allowed_keys, (
                f"{path.name}: cheia `{key}` poartă text ({value[:60]!r}). Artefactul e agregat: "
                "o cheie nouă cu string e un loc în care poate ajunge conținut de conversație."
            )
            assert "@" not in value or "." not in value.split("@")[-1], f"{path.name}: {value!r}"
            if not iso.match(value):
                assert not re.search(r"\+?\d[\d ().\-]{8,}", value), (
                    f"telefon? {path.name}: {value!r}"
                )


def test_artefactul_nu_are_identificatori_de_conversatie():
    """`business_id` e legitim (raportul e per tenant). `conversation_id`/`turn_id`/`contact_id` nu
    sunt: un raport agregat care poartă un id de conversație face agregarea reversibilă."""
    for path in _artifacts():
        raw = path.read_text(encoding="utf-8")
        for forbidden in ("conversation_id", "turn_id", "contact_id", "external_id"):
            assert forbidden not in raw, f"{path.name} conține {forbidden}"


def test_baseline_ul_exista_si_declara_fereastra():
    """O cifră fără fereastră nu se poate compara cu nimic. Baseline-ul e punctul de pornire: dacă
    n-are `window`, a doua rulare n-are cu ce să se măsoare."""
    artifacts = _artifacts()
    assert artifacts, "baseline-ul NX-272 lipsește din reports/"
    for path in artifacts:
        payload = json.loads(path.read_text(encoding="utf-8"))
        assert payload["window"]["from"] and payload["window"]["to"]
        assert payload["metrics"], "un raport fără metrici nu e un raport"
        for metric in payload["metrics"]:
            assert metric["verdict"] in {
                VERDICT_PASS,
                VERDICT_FAIL,
                VERDICT_INSUFFICIENT,
                VERDICT_UNKNOWN,
                "MEASURED",
            }, metric


def test_documentul_spune_ce_faci_cand_cifra_se_misca():
    """O cifră fără reacție declarată e o cifră pe care o vezi și treci mai departe — adică exact
    starea de dinaintea cardului, doar cu mai mult JSON."""
    doc = DOC.read_text(encoding="utf-8")
    payload = json.loads(_artifacts()[0].read_text(encoding="utf-8"))
    for metric in payload["metrics"]:
        name = metric["metric"].split(".")[0]
        assert name in doc, f"metrica `{name}` nu e explicată în QUALITY-WATCH.md"


def _strings(node, key: str = "(rădăcină)") -> list[tuple[str, str]]:
    """Fiecare string din artefact, cu CHEIA sub care stă. Cheia e jumătatea care contează: fără
    ea, un test pe valori nu poate spune diferența dintre o notă scrisă de noi și un mesaj de
    client, fiindcă la nivel de caractere nu există niciuna."""
    if isinstance(node, str):
        return [(key, node)]
    if isinstance(node, dict):
        return [pair for k, v in node.items() for pair in _strings(v, k)]
    if isinstance(node, list):
        return [pair for item in node for pair in _strings(item, key)]
    return []
