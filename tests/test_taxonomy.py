"""NX-72/NX-124 — taxonomie concern→cheie din DomainPack (clasificator determinist, fără LLM)."""

from src.domain.pack import DomainPack
from src.tools.taxonomy import _BEAUTY, _norm, map_concerns, split_concerns

# DomainPack demo (= seed beauty_salon.json). Cheile concern_map sunt deja normalizate (`_BEAUTY`).
_BEAUTY_PACK = DomainPack(vertical="beauty_salon", concern_map=_BEAUTY)
# Vertical NON-beauty cu concern_map propriu — dovada genericității (NX-124).
_HVAC_PACK = DomainPack(
    vertical="hvac", concern_map={"zgomotos": "low_noise", "economic": "energy_saving"}
)


def test_map_known_concern():
    assert map_concerns(_BEAUTY_PACK, ["ten gras"]) == ["oily"]
    assert map_concerns(_BEAUTY_PACK, ["piele sensibilă"]) == ["sensitive"]
    assert map_concerns(_BEAUTY_PACK, ["acnee"]) == ["acne"]


def test_generic_vertical_maps_from_domain_pack():
    # NX-124: orice vertical cu concern_map seedat mapează — nu mai e beauty-only.
    assert map_concerns(_HVAC_PACK, ["zgomotos"]) == ["low_noise"]
    assert map_concerns(_HVAC_PACK, ["economic", "zgomotos"]) == ["energy_saving", "low_noise"]
    # un concern beauty pe pack HVAC → necunoscut → ignorat (fără filtru fals)
    assert map_concerns(_HVAC_PACK, ["ten gras"]) == []


def test_unknown_concern_ignored():
    # Necunoscut → ignorat (NU produce filtru fals care ar goli rezultatul).
    assert map_concerns(_BEAUTY_PACK, ["frigider"]) == []
    assert map_concerns(_BEAUTY_PACK, ["ten gras", "frigider"]) == ["oily"]


def test_normalization_case_and_diacritics():
    assert _norm("Ten Grăs") == "ten gras"
    assert map_concerns(_BEAUTY_PACK, ["TEN GRAS"]) == ["oily"]
    assert map_concerns(_BEAUTY_PACK, ["Piele Grasă"]) == ["oily"]


def test_dedupe_and_stable_order():
    # Sinonime către aceeași cheie → unic; ordine stabilă (sortată).
    assert map_concerns(_BEAUTY_PACK, ["ten gras", "piele grasă"]) == ["oily"]
    assert map_concerns(_BEAUTY_PACK, ["riduri", "acnee"]) == ["acne", "anti_aging"]


def test_empty_and_none():
    assert map_concerns(_BEAUTY_PACK, None) == []
    assert map_concerns(_BEAUTY_PACK, []) == []


def test_no_pack_or_empty_map_returns_empty():
    # DomainPack lipsă (None) sau fără concern_map → fără mapare, fără crash (P6).
    assert map_concerns(None, ["ten gras"]) == []
    assert map_concerns(DomainPack(vertical="other"), ["oily"]) == []


# --- NX-227: split_concerns — aceeași politică de filtrare, pierderea devine vizibilă ----------


def test_split_returns_mapped_and_unmapped():
    mapped, unmapped = split_concerns(_BEAUTY_PACK, ["ten gras", "ten reactiv-x"])
    assert mapped == ["oily"]  # filtrul e IDENTIC cu cel de dinainte
    assert unmapped == ["ten reactiv-x"]  # ... dar termenul căzut nu mai dispare tăcut


def test_split_all_mapped_has_no_unmapped():
    # Anti-zgomot: nimic de raportat când vocabularul acoperă cererea.
    assert split_concerns(_BEAUTY_PACK, ["ten gras", "acnee"]) == (["acne", "oily"], [])


def test_split_all_unmapped_keeps_filter_empty():
    # Comportamentul de azi (fără filtru fals) + semnalul complet.
    assert split_concerns(_HVAC_PACK, ["ten gras", "frigider"]) == ([], ["frigider", "ten gras"])


def test_split_without_pack_reports_every_term():
    # Golul de CONFIGURARE e semnalul, nu o excepție de tăcut.
    assert split_concerns(None, ["ten gras"]) == ([], ["ten gras"])
    assert split_concerns(DomainPack(vertical="other"), ["oily"]) == ([], ["oily"])


def test_split_normalizes_and_dedupes_unmapped():
    # Diacritice/majuscule diferite = ACELAȘI termen → un singur rând de raportat.
    _, unmapped = split_concerns(_BEAUTY_PACK, ["ten reactív-x", "Ten Reactiv-X"])
    assert unmapped == ["ten reactiv-x"]


def test_split_drops_terms_that_normalize_to_empty():
    # „  " / emoji → nu e vocabular, nu intră nicăieri (nici filtru, nici raport).
    assert split_concerns(_BEAUTY_PACK, ["  ", "🙂", "ten gras"]) == (["oily"], [])


def test_split_empty_input():
    assert split_concerns(_BEAUTY_PACK, None) == ([], [])
    assert split_concerns(_BEAUTY_PACK, []) == ([], [])


def test_map_concerns_still_returns_only_mapped():
    # Anti-regresie pentru apelanții existenți: wrapper-ul întoarce exact ce întorcea.
    assert map_concerns(_BEAUTY_PACK, ["ten gras", "frigider"]) == ["oily"]
    assert map_concerns(None, ["ten gras"]) == []
