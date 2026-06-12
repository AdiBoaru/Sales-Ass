"""Smoke test al structurii: fiecare pachet din src/ trebuie să se importe.

Canarul pentru importuri circulare și pachete fără __init__.py — pică devreme,
nu la runtime în producție.
"""

import importlib

import pytest

PACKAGES = [
    "src",
    "src.webhook",
    "src.worker",
    "src.worker.stages",
    "src.tools",
    "src.agent",
    "src.db",
    "src.db.queries",
    "src.proactive",
    "src.gdpr",
    "src.jobs",
]


@pytest.mark.parametrize("name", PACKAGES)
def test_package_imports(name):
    assert importlib.import_module(name) is not None


def test_version_exposed():
    import src

    assert isinstance(src.__version__, str)
