"""NX-380 — notițele de magazin pentru interpretare sunt DATE ale pachetului, validate la încărcare.

Instrucțiunile adaptorului sunt generice (P11, poarta I14), deci ce ține de catalogul unui tenant
intră în prompt din `businesses.settings.domain_pack.interpret_notes`, sub `STORE NOTES`."""

from __future__ import annotations

import json
import pathlib

from src.domain import loader
from src.domain.loader import (
    MAX_INTERPRET_NOTE_CHARS,
    MAX_INTERPRET_NOTES,
    _norm_interpret_notes,
    load_domain_pack,
)
from src.domain.pack import interpret_notes
from src.models import BusinessConfig

ROOT = pathlib.Path(__file__).resolve().parents[1]
SOLE_PACK = ROOT / "db" / "seed" / "domain_pack_sole_ro.json"


def test_notes_are_kept_per_locale_trimmed_and_in_order():
    out = _norm_interpret_notes({"RO": ["  întâi  ", "apoi"], "en": ["first"]})
    assert out == {"ro": ("întâi", "apoi"), "en": ("first",)}


def test_a_note_with_a_line_break_or_too_long_is_dropped_alone(caplog):
    long = "x" * (MAX_INTERPRET_NOTE_CHARS + 1)
    out = _norm_interpret_notes({"ro": ["bună", "rupe\nblocul", long, "", 7, "și asta"]})
    assert out == {"ro": ("bună", "și asta")}
    assert "interpret_notes" in caplog.text


def test_notes_are_capped():
    out = _norm_interpret_notes({"ro": [f"n{i}" for i in range(MAX_INTERPRET_NOTES + 5)]})
    assert len(out["ro"]) == MAX_INTERPRET_NOTES


def test_garbage_gives_no_notes():
    assert _norm_interpret_notes(None) == {}
    assert _norm_interpret_notes({"ro": "nu e listă"}) == {}
    assert _norm_interpret_notes(["ro"]) == {}


def test_the_loader_carries_the_notes_into_the_pack():
    biz = BusinessConfig(
        id="b",
        slug="s",
        name="n",
        vertical="ecommerce",
        settings={"domain_pack": {"interpret_notes": {"ro": ["o notiță"]}}},
    )
    pack = load_domain_pack(biz)
    assert interpret_notes(pack, "ro-RO") == ("o notiță",)
    assert interpret_notes(pack, "en") == ()
    assert interpret_notes(None, "ro") == ()


def test_the_sole_pack_notes_load_whole():
    """Notițele SOLE din seed trec toate de loader: niciuna nu se pierde tăcut la aplicare."""
    raw = json.loads(SOLE_PACK.read_text(encoding="utf-8"))["interpret_notes"]
    kept = _norm_interpret_notes(raw)
    assert {k: len(v) for k, v in kept.items()} == {k: len(v) for k, v in raw.items()}
    assert len(kept["ro"]) >= 1


def test_the_default_packs_declare_no_notes():
    """Notițele sunt ale unui TENANT, nu ale unui vertical: niciun default nu le poartă."""
    for path in (ROOT / "src" / "domain" / "defaults").glob("*.json"):
        assert "interpret_notes" not in json.loads(path.read_text(encoding="utf-8")), path.name
    assert loader.MAX_INTERPRET_NOTES <= 20
