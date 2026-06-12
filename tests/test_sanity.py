"""Smoke test — verifică că pachetele principale se importă fără erori."""

import src
import src.agent
import src.db
import src.proactive
import src.tools
import src.webhook
import src.worker
import src.worker.stages


def test_imports():
    assert src is not None


def test_version():
    assert hasattr(src, "__version__")
