"""NX-248 — identitatea artefactului, livrarea secretelor și heartbeat-ul proceselor non-HTTP."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

from src.config import Settings, get_settings
from src.ops import build_info as bi
from src.ops import worker_health as wh

# ── Identitatea artefactului ─────────────────────────────────────────────────────────────────


def test_schema_requires_e_derivat_din_migrarile_prezente(tmp_path: Path):
    """Derivat, nu declarat: cine adaugă `043_*.sql` nu poate uita să actualizeze un număr."""
    for name in ("003_x.sql", "042_y.sql", "not_a_migration.sql"):
        (tmp_path / name).write_text("-- ", encoding="utf-8")
    assert bi.bundled_schema_version(tmp_path) == 42
    (tmp_path / "043_z.sql").write_text("-- ", encoding="utf-8")
    assert bi.bundled_schema_version(tmp_path) == 43


def test_intervalul_de_schema_e_requires_plus_toleranta(tmp_path: Path):
    (tmp_path / "042_y.sql").write_text("-- ", encoding="utf-8")
    info = bi.build_info(Settings(), role="api", docs_dir=tmp_path)
    assert info.schema_requires == 42
    assert info.schema_tolerates == 42 + bi.SCHEMA_FORWARD_TOLERANCE
    assert info.tolerates_schema(42) and info.tolerates_schema(43)
    assert not info.tolerates_schema(41), "schema prea veche: imaginea cere ce DB-ul n-are"
    assert not info.tolerates_schema(44), "schema prea nouă: rollback orb pe coloane necunoscute"


def test_digestul_pretins_e_respins_daca_nu_e_digest(monkeypatch, tmp_path: Path):
    """Un TAG în câmpul de digest e chiar bugul pe care cardul îl numește: `latest` mutat."""
    monkeypatch.setenv("IMAGE_DIGEST", "latest")
    assert (
        bi.build_info(Settings(), role="api", docs_dir=tmp_path).image_digest_claimed == "unknown"
    )
    monkeypatch.setenv("IMAGE_DIGEST", "sha256:" + "a" * 64)
    assert bi.build_info(Settings(), role="api", docs_dir=tmp_path).image_digest_claimed.startswith(
        "sha256:"
    )


def test_built_at_invalid_nu_devine_now(monkeypatch, tmp_path: Path):
    """`now()` ca fallback ar face fiecare restart să arate ca un build nou."""
    monkeypatch.setenv("BUILT_AT", "acum")
    assert bi.build_info(Settings(), role="api", docs_dir=tmp_path).built_at == "unknown"


def test_config_revision_e_stabila_si_se_schimba_la_comportament(monkeypatch, tmp_path: Path):
    first = bi.config_revision(Settings())
    assert first == bi.config_revision(Settings()), "aceeași config ⇒ aceeași revizie"
    monkeypatch.setenv("WEB_RATE_LIMIT_MAX_IP", "999")
    get_settings.cache_clear()
    assert bi.config_revision(Settings()) != first
    get_settings.cache_clear()


def test_config_revision_nu_se_misca_la_schimbarea_unui_secret(monkeypatch):
    """Un secret nu descrie comportamentul. Dacă ar intra în amprentă, rotația unei chei ar arăta
    în manifest ca o schimbare de config — și ar invita pe cineva să caute diferența."""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-aaa")
    first = bi.config_revision(Settings())
    monkeypatch.setenv("OPENAI_API_KEY", "sk-bbb")
    assert bi.config_revision(Settings()) == first


@pytest.mark.parametrize(
    ("field", "secret"),
    [
        ("openai_api_key", True),
        ("supabase_db_url", True),
        ("database_url_bot", True),
        ("redis_url", True),
        ("web_action_keys", True),
        ("meta_app_secret", True),
        ("ops_health_token", True),
        ("web_session_secret_ttl_s", False),
        ("web_cors_origins", False),
        ("model_agent", False),
        ("web_enabled", False),
    ],
)
def test_clasificarea_secretelor(field, secret):
    assert bi.is_secret_field(field) is secret


#: Verdictul FIXAT pentru fiecare câmp secret din `Settings`. Nu e o dublare a clasificatorului,
#: e o poartă: un câmp nou care arată a secret pică testul până când cineva îl adaugă AICI, adică
#: până când cineva decide conștient dacă intră sau nu în amprenta de config și în loguri.
SECRET_FIELDS = frozenset(
    {
        "database_url_bot",
        "database_url_migration",
        "meta_access_token",
        "meta_app_secret",
        "meta_verify_token",
        "observability_trace_secret",
        "openai_api_key",
        "ops_health_token",
        "orders_webhook_secret",
        "redis_url",
        "retrieval_decision_key",
        "supabase_db_url",
        "telegram_bot_token",
        "web_action_keys",
        "web_demo_access_secret",
        "web_feedback_prompt_secret",
        "web_turn_fingerprint_secret",
        "supabase_db_url",
    }
)


def test_inventarul_de_secrete_e_complet_si_intentionat():
    """Poarta care face clasificarea imposibil de uitat: dacă cineva adaugă
    `PARTNER_API_KEY` în `Settings`, testul pică până când e trecut în inventar."""
    detected = {name for name in Settings.model_fields if bi.is_secret_field(name)}
    assert detected == SECRET_FIELDS, (
        "inventarul de secrete a divergat de clasificator; adaugă câmpul nou în SECRET_FIELDS "
        "(și verifică dacă are nevoie de rotație în docs/SECRETS-ROTATION.md)"
    )


def test_plafoanele_de_tokeni_nu_sunt_confundate_cu_secrete():
    """`llm_max_tokens_agent` conține „token" dar e o decizie de comportament: dacă ar fi
    clasificat secret, o schimbare de plafon n-ar mai muta `config_revision`, deci două deployuri
    care se comportă diferit ar arăta identic în manifest."""
    token_caps = [n for n in Settings.model_fields if "tokens" in n]
    assert token_caps, "așteptam cel puțin un plafon de tokeni în Settings"
    assert not any(bi.is_secret_field(n) for n in token_caps)


def test_niciun_secret_nu_apare_in_amprenta(monkeypatch):
    """Testul de canary: punem un marker în FIECARE câmp secret și îl căutăm în tot ce compune
    identitatea publică a artefactului."""
    canary = "CANARY248"
    monkeypatch.setenv("OPENAI_API_KEY", f"sk-{canary}")
    monkeypatch.setenv("META_APP_SECRET", canary)
    monkeypatch.setenv("OPS_HEALTH_TOKEN", canary)
    settings = Settings()
    payload = json.dumps(bi.build_info(settings, role="api").operator())
    assert canary not in payload
    # și nici materialul din care se calculează amprenta nu-l conține
    assert canary not in bi.config_revision(settings)


# ── Livrarea secretelor prin fișier ──────────────────────────────────────────────────────────


def test_secretul_vine_din_fisier(monkeypatch, tmp_path: Path):
    secret = tmp_path / "ops_token"
    secret.write_text("valoare-din-fisier\n", encoding="utf-8")  # `\n` de la `echo`
    monkeypatch.delenv("OPS_HEALTH_TOKEN", raising=False)
    monkeypatch.setenv("OPS_HEALTH_TOKEN_FILE", str(secret))
    assert Settings().ops_health_token == "valoare-din-fisier"


def test_env_si_file_impreuna_opresc_procesul(monkeypatch, tmp_path: Path):
    """„Fișierul câștigă" pare prietenos până când cineva rotește secretul în fișier, uită env-ul
    vechi și jumătate din flotă rulează cu credentialul revocat — tăcut."""
    secret = tmp_path / "ops_token"
    secret.write_text("nou", encoding="utf-8")
    monkeypatch.setenv("OPS_HEALTH_TOKEN", "vechi")
    monkeypatch.setenv("OPS_HEALTH_TOKEN_FILE", str(secret))
    with pytest.raises(ValueError, match="AMBELE"):
        Settings()


def test_fisier_de_secret_absent_e_fail_closed(monkeypatch, tmp_path: Path):
    monkeypatch.delenv("OPS_HEALTH_TOKEN", raising=False)
    monkeypatch.setenv("OPS_HEALTH_TOKEN_FILE", str(tmp_path / "nu-exista"))
    with pytest.raises(ValueError):
        Settings()


# ── Heartbeat pentru procesele fără HTTP ─────────────────────────────────────────────────────


def _hb(**kw) -> wh.Heartbeat:
    base = {
        "role": "worker",
        "pid": os.getpid(),
        "boot_id": wh.BOOT_ID,
        "release_sha": "abc1234",
        "last_success": time.time(),
    }
    return wh.Heartbeat(**{**base, **kw})


def test_heartbeat_scris_si_citit(tmp_path: Path):
    path = tmp_path / "hb"
    wh.write(_hb(queue_lag_bucket="empty"), path)
    loaded = wh.read(path)
    assert loaded is not None and loaded.role == "worker" and loaded.queue_lag_bucket == "empty"


def test_heartbeat_vechi_e_stale(tmp_path: Path):
    hb = _hb(last_success=time.time() - 500)
    assert wh.evaluate(hb, max_age_s=90).reason == "stale"


def test_fisier_proaspat_de_la_proces_mort_e_detectat(tmp_path: Path):
    """Ăsta e chiar cazul din card: „un fișier heartbeat scris de un process mort nu este
    suficient". PID-ul 1 dintr-un container gol nu există în procesul de test; pe Windows
    verificarea de PID nu e portabilă, deci acolo dovada e `boot_id`."""
    hb = _hb(pid=999_999, boot_id="alt-boot")
    verdict = wh.evaluate(hb, max_age_s=90, own_boot_id=wh.BOOT_ID)
    assert verdict.ok is False
    assert verdict.reason in ("dead_process", "foreign_boot")


def test_heartbeat_din_viitor_nu_e_proaspat():
    """Un ceas sărit ar face un fișier vechi „valabil pentru totdeauna"."""
    assert wh.evaluate(_hb(last_success=time.time() + 10_000), max_age_s=90).reason == "clock_skew"


def test_bucla_de_lease_moarta_si_schema_incompatibila_sunt_nesanatoase():
    assert wh.evaluate(_hb(lease_loop_alive=False), max_age_s=90).reason == "lease_loop_dead"
    assert wh.evaluate(_hb(schema_compatible=False), max_age_s=90).reason == "schema_incompatible"


def test_lipsa_fisierului_nu_e_sanatate():
    assert wh.evaluate(None, max_age_s=90).reason == "no_heartbeat"


def test_json_stricat_nu_crapa_sonda(tmp_path: Path):
    """O sondă care aruncă la JSON trunchiat ar reporni un proces sănătos surprins la mijlocul
    unei scrieri (de-asta scrierea e tmp+rename, iar citirea e tolerantă)."""
    path = tmp_path / "hb"
    path.write_text('{"role": "worker", "pid"', encoding="utf-8")
    assert wh.read(path) is None


def test_cli_intoarce_cod_si_motiv(tmp_path: Path, capsys):
    path = tmp_path / "hb"
    wh.write(_hb(), path)
    assert wh.main(["--role", "worker", "--path", str(path), "--max-age", "90"]) == 0
    assert json.loads(capsys.readouterr().out)["reason"] == "ok"
    wh.write(_hb(last_success=time.time() - 5000), path)
    assert wh.main(["--role", "worker", "--path", str(path), "--max-age", "90"]) == 1
    assert json.loads(capsys.readouterr().out)["reason"] == "stale"


def test_bucketul_de_lag_nu_expune_adancimea_exacta():
    """Adâncimea e o valoare de client (câți vizitatori așteaptă acum); bucketul e o stare."""
    assert wh.lag_bucket(None) == "unknown"
    assert wh.lag_bucket(0) == "empty"
    assert wh.lag_bucket(3) == "low"
    assert wh.lag_bucket(5000) == "high"
    assert set(wh.QUEUE_LAG_BUCKETS) == {"unknown", "empty", "low", "high"}
