"""Fixture-uri partajate de teste.

NX-78: `agent_stage` citește categorii + aliase din DB pt promptul generat din `categories`
(principiul 9). Testele care exersează pipeline-ul/agentul folosesc un `conn` fals
(`object()`), deci stubbim cele două query-uri global → prompt generic, fără DB reală.
Testele care vor să verifice CONȚINUTUL promptului (ex. test_agent vertical-capture) îl
suprascriu cu o fixtură locală mai specifică.
"""

import base64

import pytest

from src.worker.stages import agent as agent_mod


@pytest.fixture(autouse=True)
def _stub_agent_prompt_inputs(monkeypatch):
    async def _no_categories(conn, business_id):
        return []

    async def _no_aliases(conn, business_id, **kwargs):
        return []

    monkeypatch.setattr(agent_mod, "list_category_names", _no_categories, raising=False)
    monkeypatch.setattr(agent_mod, "list_routing_aliases", _no_aliases, raising=False)


def tamper_token(token: str) -> str:
    """Strică SIGILIUL unui token de acțiune (NX-236), nu textul care îl transportă.

    Varianta evidentă — `token[:-2] + "AB"` — NU strică nimic în mod fiabil, fiindcă base64 are
    biți nesemnificativi la coadă. Corpul sigilat are 475 de caractere (475 % 4 == 3), deci
    ultimul caracter poartă doar 4 biți utili din 6: cei 2 de jos se aruncă la decodare. Când
    octeții afectați erau deja zero, tokenul „stricat" decoda la ACEIAȘI bytes, trecea de AES-SIV
    și ajungea la accept — adică testul verifica opusul a ce credea că verifică.

    Măsurat pe tokenuri emise real: 34 din 40.000 (0,085%), adică o rulare de CI la ~1.200. A
    picat `main` pe 2026-08-19 (run 32262284942), unde arăta ca o regresie de securitate.

    Flipul se face pe BYTES, în primul octet — care e din tagul SIV — deci verificarea pică
    întotdeauna, indiferent de lungime sau de conținut.
    """
    version, key_id, body = token.split(".", 2)
    raw = bytearray(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
    raw[0] ^= 0x01
    flipped = base64.urlsafe_b64encode(bytes(raw)).decode("ascii").rstrip("=")
    return f"{version}.{key_id}.{flipped}"


@pytest.fixture
def tamper_action_token():
    """Vezi `tamper_token`: un tamper care chiar strică, disponibil și în `tests/e2e/`."""
    return tamper_token
