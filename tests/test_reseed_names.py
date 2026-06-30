"""Val2 — logica pură de curățare a ID-ului rezidual din numele de produs (scripts/).

Importăm helperele pure din scriptul de re-seed (importul de DB e lazy în `run()`, deci încărcarea
modulului nu atinge nicio conexiune). Testăm DOAR funcțiile deterministe: identificarea precisă a
ID-ului din slug + strip idempotent + păstrarea numerelor legitime.
"""

import importlib.util
import pathlib

_spec = importlib.util.spec_from_file_location(
    "reseed_product_names", pathlib.Path("scripts/reseed_product_names.py")
)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)
clean_name = _mod.clean_name
residual_id = _mod.residual_id


def test_residual_id_from_slug():
    assert residual_id("ardent-lab-masca-pentru-hidratare-348-474c8dae") == "348"
    assert residual_id("aurelia-lab-pensula-pentru-calmare-053-38962719") == "053"
    assert residual_id("crema-spf-50-cream") is None  # fără hash final → nu e ID rezidual
    assert residual_id(None) is None


def test_clean_name_strips_residual():
    assert (
        clean_name(
            "Ardent Lab Masca pentru hidratare 348",
            "ardent-lab-masca-pentru-hidratare-348-474c8dae",
        )
        == "Ardent Lab Masca pentru hidratare"
    )
    assert (
        clean_name(
            "Aurelia Lab Pensula pentru calmare 053",
            "aurelia-lab-pensula-pentru-calmare-053-38962719",
        )
        == "Aurelia Lab Pensula pentru calmare"
    )


def test_clean_name_idempotent():
    # deja curat (fără coada de ID) → neschimbat, deci re-rularea e no-op
    assert (
        clean_name(
            "Ardent Lab Masca pentru hidratare",
            "ardent-lab-masca-pentru-hidratare-348-474c8dae",
        )
        == "Ardent Lab Masca pentru hidratare"
    )


def test_clean_name_preserves_legit_number():
    # slug fără tiparul de ID rezidual → numărul legit „50" rămâne în nume
    assert clean_name("Crema SPF 50", "crema-spf-50-cream") == "Crema SPF 50"
