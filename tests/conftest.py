"""Fixture-uri partajate de teste.

NX-78: `agent_stage` citește categorii + aliase din DB pt promptul generat din `categories`
(principiul 9). Testele care exersează pipeline-ul/agentul folosesc un `conn` fals
(`object()`), deci stubbim cele două query-uri global → prompt generic, fără DB reală.
Testele care vor să verifice CONȚINUTUL promptului (ex. test_agent vertical-capture) îl
suprascriu cu o fixtură locală mai specifică.

NX-275 — de ce suita NU citește `.env`-ul dezvoltatorului. `Settings.model_config` rezolvă
`env_file` la IMPORT, iar `.env`-ul local are stiva de flag-uri APRINSĂ (single brain, transport
v2, acțiuni, coș, observabilitate). Cu el citit, 108 teste picau local și treceau în CI — nu
fiindcă ar fi fost stricate, ci fiindcă verificau comportamentul cu flagul stins într-un mediu în
care era aprins. Suita trebuie să măsoare INVARIANTUL, nu configurația celui care o rulează.

Testele care vor profilul aprins îl declară ele (`monkeypatch.setenv` + `get_settings.cache_clear`).
Cine rulează testele `integration`, care au nevoie de DSN-ul real, pune `NX_TESTS_READ_ENV_FILE=1`.
"""

import os

# ÎNAINTEA oricărui import din `src`: altfel `Settings` s-a definit deja cu `.env` citit.
#
# Valorile de mai jos sunt EXACT cele pe care le pune `ci.yml` (jobul `Test (pytest)`). Scopul e ca
# o rulare locală să reproducă CI bit cu bit: câteva module construiesc `Settings()` la import și au
# nevoie de câmpurile obligatorii, care până acum veneau din `.env`. `setdefault`, nu atribuire: o
# variabilă exportată explicit de cel care rulează suita rămâne câștigătoare.
if not os.getenv("NX_TESTS_READ_ENV_FILE"):
    os.environ["NX_CONFIG_ENV_FILE"] = ""
    for _key, _value in {
        "OPENAI_API_KEY": "test-key",
        "META_ACCESS_TOKEN": "test-token",
        "META_APP_SECRET": "test-secret",
        "META_VERIFY_TOKEN": "test-verify",
        "META_PHONE_NUMBER_ID": "000000000",
        "SUPABASE_DB_URL": "postgresql://test:test@localhost/test",
        "REDIS_URL": "redis://localhost:6379/0",
        "ENV": "test",
        "LOG_LEVEL": "WARNING",
        "DAILY_COST_CAP_USD": "5",
    }.items():
        os.environ.setdefault(_key, _value)

import base64  # noqa: E402

import pytest  # noqa: E402

from src.worker.stages import agent as agent_mod  # noqa: E402


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
