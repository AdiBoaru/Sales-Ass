"""Teste unit pentru Settings — fără DB, deterministe (env injectat)."""

from src.config import Settings

BASE_ENV = {
    "SUPABASE_DB_URL": "postgresql://u:p@host:5432/db",
    "OPENAI_API_KEY": "sk-test",
    "META_VERIFY_TOKEN": "verify-123",
}


def _settings(monkeypatch, **overrides):
    for k, v in {**BASE_ENV, **overrides}.items():
        monkeypatch.setenv(k, v)
    # _env_file=None → ignoră .env local, test determinist
    return Settings(_env_file=None)


def test_reads_required_and_optional(monkeypatch):
    s = _settings(monkeypatch)
    assert s.supabase_db_url == "postgresql://u:p@host:5432/db"
    assert s.openai_api_key == "sk-test"
    assert s.meta_verify_token == "verify-123"


def test_defaults(monkeypatch):
    # testează valorile IMPLICITE → șterge variabilele opționale ca să nu se
    # scurgă mediul (CI le setează ca env de job)
    for k in (
        "MODEL_AGENT",
        "MODEL_TRIAGE",
        "MODEL_EMBED",
        "REDIS_URL",
        "DAILY_COST_CAP_USD",
        "ENV",
        "LOG_LEVEL",
    ):
        monkeypatch.delenv(k, raising=False)
    s = _settings(monkeypatch)
    assert s.model_agent == "gpt-5.4-mini"
    assert s.model_triage == "gpt-5.4-nano"
    assert s.model_embed == "text-embedding-3-small"
    assert s.redis_url == "redis://redis:6379/0"
    assert s.daily_cost_cap_usd == 5.0
    assert s.env == "dev"


def test_database_url_alias(monkeypatch):
    """Compat: DATABASE_URL e acceptat ca alias pentru SUPABASE_DB_URL."""
    monkeypatch.delenv("SUPABASE_DB_URL", raising=False)
    monkeypatch.setenv("DATABASE_URL", "postgresql://x:y@h:5432/d")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-x")
    s = Settings(_env_file=None)
    assert s.supabase_db_url == "postgresql://x:y@h:5432/d"


def test_is_prod(monkeypatch):
    assert _settings(monkeypatch, ENV="prod").is_prod is True
    assert _settings(monkeypatch, ENV="dev").is_prod is False
