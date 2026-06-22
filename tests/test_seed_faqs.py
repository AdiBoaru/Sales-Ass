"""NX-74 (extra) — jobul seed_faqs: generare + populare `faqs`. ZERO DB/OpenAI real.

Fake conn (transaction/fetchval/execute) + mock LLM. Acoperă: bază curatată inserată +
embed-uită, idempotență (existing → update), fără LLM (embedding NULL, tot inserează),
generare LLM adăugată + dedupe.
"""

from src.jobs import seed_faqs as sf


class _LLM:
    def __init__(self, *, gen=None):
        self._gen = gen

    async def embed(self, texts, *, model=None):
        return [[0.1, 0.2, 0.3] for _ in texts]

    async def complete_schema(self, system, user, schema, *, model=None):
        return {"faqs": self._gen or []}


class _NoopTx:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


class _FakeConn:
    """Conn minimal: transaction() no-op, fetchval (existing?), execute (insert/update)."""

    def __init__(self, existing: set[tuple[str, str]] | None = None):
        self.existing = existing or set()  # (question, locale) deja în DB
        self.inserts: list[tuple] = []
        self.updates: list[tuple] = []

    def transaction(self):
        return _NoopTx()

    async def fetchval(self, sql, business_id, question, locale):
        return "existing-id" if (question, locale) in self.existing else None

    async def execute(self, sql, *args):
        if sql.lstrip().startswith("update"):
            self.updates.append(args)
        else:
            self.inserts.append(args)


async def test_base_seed_creates_and_embeds():
    conn = _FakeConn()
    stats = await sf.seed_faqs(conn, _LLM(), "biz-1", locale="ro")
    assert stats["created"] == len(sf.BASE_FAQS_RO)
    assert stats["updated"] == 0
    assert stats["embedded"] == len(sf.BASE_FAQS_RO)
    # fiecare insert are un vector pgvector ne-nul pe poziția embedding (penultimul; ultimul =
    # embedding_model, NX-124a)
    assert all(args[-2] is not None and args[-2].startswith("[") for args in conn.inserts)
    assert all(args[-1] == "text-embedding-3-small" for args in conn.inserts)  # modelul stocat


async def test_idempotent_existing_updates_not_duplicates():
    first_q = sf.BASE_FAQS_RO[0][0]
    conn = _FakeConn(existing={(first_q, "ro")})
    stats = await sf.seed_faqs(conn, _LLM(), "biz-1", locale="ro")
    assert stats["updated"] == 1
    assert stats["created"] == len(sf.BASE_FAQS_RO) - 1
    assert len(conn.updates) == 1 and len(conn.inserts) == len(sf.BASE_FAQS_RO) - 1


async def test_no_llm_inserts_with_null_embedding():
    conn = _FakeConn()
    stats = await sf.seed_faqs(conn, None, "biz-1", locale="ro")
    assert stats["created"] == len(sf.BASE_FAQS_RO) and stats["embedded"] == 0
    assert all(args[-2] is None for args in conn.inserts)  # embedding NULL fără cheie (penultim)


async def test_generate_adds_and_dedupes():
    gen = [
        {"question": "Aveți tester la parfumuri?", "answer": "Da, la unele."},
        {"question": sf.BASE_FAQS_RO[0][0], "answer": "duplicat al bazei"},  # dedupe pe întrebare
        {"question": "", "answer": "fără întrebare"},  # ignorat
    ]
    conn = _FakeConn()
    stats = await sf.seed_faqs(conn, _LLM(gen=gen), "biz-1", generate=True, generate_n=3)
    # baza + exact 1 FAQ nou valid (duplicatul + cel gol sunt eliminate)
    assert stats["created"] == len(sf.BASE_FAQS_RO) + 1


async def test_generate_llm_failure_falls_back_to_base():
    class _BoomLLM(_LLM):
        async def complete_schema(self, *a, **k):
            raise RuntimeError("API down")

    conn = _FakeConn()
    stats = await sf.seed_faqs(conn, _BoomLLM(), "biz-1", generate=True)
    assert stats["created"] == len(sf.BASE_FAQS_RO)  # generarea a picat → doar baza
