import pytest

from src.safety.external_data import external_query_text


@pytest.mark.parametrize(
    "query",
    [
        "ser 0712 345 678 pentru ten gras",
        "scrie la ion@example.ro pentru o crema",
        "iban ro49aaaa1b31007593840000 crema",
        "strada florilor 5 caut un ser",
        "ma numesc ion popescu caut o crema",
        "ma cheama maria vreau un fond",
        "numele meu este ana, ce crema imi recomanzi",
        "4111 1111 1111 1111",
    ],
)
def test_external_query_rejects_person_identifiers(query):
    """Ce apărăm e IDENTITATEA: telefon, email, IBAN, card, adresă, nume declarat."""
    assert external_query_text(query) is None


@pytest.mark.parametrize(
    "query",
    [
        # „sunt X Y" — în română introduce aproape orice descriere de sine, nu un nume.
        "sunt cu ten gras ce fond imi recomanzi",
        "sunt in cautarea unui ser cu vitamina c",
        "sunt foarte multumita de crema",
        # subiecte de sănătate: descriu o NEVOIE, nu o persoană
        "sunt insarcinata caut o crema",
        "am o afectiune a scalpului",
        "ceva pentru alergie severa",
        "crema pentru ten sensibil si rozacee",
    ],
)
def test_external_query_allows_self_description_and_health_needs(query):
    """Regresie NX-209 — garda bloca 9 din 10 interogări reale de beauty.

    Cazul cel mai grav e „sunt însărcinată": e exact interogarea pentru care există gate-ul de
    contraindicații (NX-173). Tăindu-i retrievalul semantic, fix cazul de siguranță primea cel mai
    slab răspuns — o protecție care înrăutățește lucrul pe care îl apără. Iar niciuna dintre
    frazele astea nu identifică pe nimeni."""
    assert external_query_text(query) is not None


def test_external_query_normalizes_and_allows_product_intent_only():
    assert (
        external_query_text("Cremă MATIFIANTĂ pentru ten gras")
        == "crema matifianta pentru ten gras"
    )


# --- P1 review #251: numele declarat cu „sunt" -------------------------------


@pytest.mark.parametrize(
    "raw",
    [
        "sunt Ion Popescu si caut o crema",
        "Sunt Maria Ionescu, ce imi recomanzi",
        "sunt Ana Maria Popa",
    ],
)
def test_declared_name_with_sunt_is_blocked_on_the_raw_text(raw):
    """`normalized_query` vine DEJA lowercase din `query_spec`, iar majuscula e exact semnalul care
    separă un nume de o descriere de sine. De aceea detecția rulează pe `detect_on` (textul brut),
    în timp ce exportul rămâne mereu `normalized_query` — brutul n-are cale de ieșire."""
    assert external_query_text(raw.lower(), detect_on=raw) is None


def test_detection_input_is_never_what_gets_exported():
    raw = "Crema MATIFIANTA pentru ten gras"
    assert external_query_text(raw.lower(), detect_on=raw) == "crema matifianta pentru ten gras"


@pytest.mark.parametrize(
    "raw",
    [
        "sunt cu ten gras, ce fond imi recomanzi",
        "Sunt foarte multumita de crema",
        "sunt insarcinata, ce crema pot folosi",
        "sunt alergica la parfum",
        "sunt Ion",  # un singur token: prenume izolat, nu nume complet declarat
    ],
)
def test_self_description_still_passes_with_raw_detection(raw):
    assert external_query_text(raw.lower(), detect_on=raw) is not None
