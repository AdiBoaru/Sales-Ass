"""NX-248 — smoke-ul de release alege contractul pe care îl SERVEȘTE instanța.

Scriptul n-avea niciun test, iar defectul pe care îl repară testele astea l-a găsit o promovare
reală: smoke-ul cerea `POST /web/v2/turns` → 202, dar v2 e în spatele unui flag stins în producție
(măsurat 2026-08-19: 404 cu parametri, 422 fără). Deployul reușea, smoke-ul pica, iar releaseul
raporta „picat" pentru o promovare care de fapt mersese — cel mai prost moment posibil pentru un
fals negativ, fiindcă imaginea era deja schimbată.

Testele nu ating rețeaua: `_request` e înlocuit cu un fals care întoarce răspunsuri scriptate.
"""

import json

import pytest

from scripts.release import smoke_web_v2 as smoke

BASE = "https://bot.example.com"
TOKEN = "public-token"

HEALTH = (200, {"status": "ok", "release": "abc123"}, '{"status":"ok"}')
BOOTSTRAP = (200, {"visitor_id": "web_1", "sig": "s"}, '{"visitor_id":"web_1"}')


@pytest.fixture(autouse=True)
def _fara_asteptare_reala(monkeypatch):
    """Așteptarea de readiness e MĂRGINITĂ în producție, dar aici o vrem instantanee: testăm
    politica (câte încercări, ce verdict), nu ceasul."""
    monkeypatch.setattr(smoke, "READY_TIMEOUT_S", 0.05)
    monkeypatch.setattr(smoke, "READY_POLL_S", 0.0)
    monkeypatch.setattr(smoke.time, "sleep", lambda _s: None)


def _wire(monkeypatch, routes):
    """`routes` = listă de (predicat_pe_url, metodă, răspuns). Prima potrivire câștigă."""
    seen: list[tuple[str, str]] = []

    def fake_request(url, *, method="GET", body=None, timeout=15.0):
        seen.append((method, url))
        for match, want_method, response in routes:
            if want_method == method and match in url:
                return response
        raise AssertionError(f"cerere neașteptată: {method} {url}")

    monkeypatch.setattr(smoke, "_request", fake_request)
    return seen


def _v1_routes(chat_response):
    return [
        ("/health/ready", "GET", HEALTH),
        ("/web/bootstrap", "GET", BOOTSTRAP),
        # v2 stins: gate-ul răspunde 404 ÎNAINTE de verificarea sesiunii.
        ("/web/v2/turns", "POST", (404, {"detail": "not found"}, '{"detail":"not found"}')),
        ("/web/chat", "POST", chat_response),
    ]


def test_v2_dark_falls_back_to_the_v1_contract_and_says_so(monkeypatch):
    """404 pe v2 ⇒ verifică v1 și DECLARĂ profilul. Un verde fără profil ar fi de necitit."""
    raw = json.dumps({"content": "Salut! Cu ce te pot ajuta?", "products": [], "suggestions": []})
    chat = (200, json.loads(raw), raw)
    seen = _wire(monkeypatch, _v1_routes(chat))

    report = smoke.run_smoke(BASE, TOKEN)

    assert report["ok"] is True
    assert report["profile"] == "v1"
    assert [s["step"] for s in report["steps"]][-3:] == [
        "chat_v1",
        "raspuns_v1_nu_e_gol",
        "raspuns_v1_nu_e_fallback_de_runner",
    ]
    # Turul sincron a fost chiar exersat, nu doar declarat.
    assert ("POST", f"{BASE}/web/chat") in seen


def test_v1_empty_answer_fails_because_silence_is_a_bug(monkeypatch):
    """P6: niciodată tăcere. Un 200 cu `content` gol e exact defectul care trebuie prins."""
    raw = json.dumps({"content": "   ", "products": []})
    seen = _wire(monkeypatch, _v1_routes((200, json.loads(raw), raw)))

    report = smoke.run_smoke(BASE, TOKEN)

    assert report["ok"] is False
    assert report["failed_step"] == "raspuns_v1_nu_e_gol"
    assert report["profile"] == "v1"
    assert ("POST", f"{BASE}/web/chat") in seen


def test_expecting_v2_makes_a_silent_fallback_to_v1_a_failure(monkeypatch):
    """După cutover, `--expect-profile v2` nu lasă un flag stins din greșeală să treacă verde."""
    raw = json.dumps({"content": "ok", "products": []})
    _wire(monkeypatch, _v1_routes((200, json.loads(raw), raw)))

    report = smoke.run_smoke(BASE, TOKEN, expect_profile="v2")

    assert report["ok"] is False
    assert report["failed_step"] == "profil_detectat"
    assert report["profile"] == "v1"


def test_v2_alive_runs_the_durable_chain_including_replay(monkeypatch):
    """Cu v2 pornit, contractul verificat e cel asincron — fără să atingă nimeni fișierul."""
    terminal = json.dumps({"turn": {"id": "t-1", "status": "completed"}, "view": {"blocks": []}})
    routes = [
        ("/health/ready", "GET", HEALTH),
        ("/web/bootstrap", "GET", BOOTSTRAP),
        ("/web/v2/turns?", "POST", (202, {"turn": {"id": "t-1"}}, "{}")),
        ("/web/v2/turns/t-1", "GET", (200, json.loads(terminal), terminal)),
    ]
    _wire(monkeypatch, routes)

    report = smoke.run_smoke(BASE, TOKEN)

    assert report["ok"] is True
    assert report["profile"] == "v2"
    steps = [s["step"] for s in report["steps"]]
    assert "replay_byte_identic" in steps
    assert "idempotenta" in steps


def test_replay_that_differs_is_caught(monkeypatch):
    """Dacă a doua citire diferă, „refresh-ul paginii" schimbă un răspuns deja dat clientului."""
    first = json.dumps({"turn": {"id": "t-1", "status": "completed"}, "n": 1})
    second = json.dumps({"turn": {"id": "t-1", "status": "completed"}, "n": 2})
    responses = iter([(200, json.loads(first), first), (200, json.loads(second), second)])

    def fake_request(url, *, method="GET", body=None, timeout=15.0):
        if "/health/ready" in url:
            return HEALTH
        if "/web/bootstrap" in url:
            return BOOTSTRAP
        if method == "POST":
            return (202, {"turn": {"id": "t-1"}}, "{}")
        return next(responses)

    monkeypatch.setattr(smoke, "_request", fake_request)

    report = smoke.run_smoke(BASE, TOKEN)

    assert report["ok"] is False
    assert report["failed_step"] == "replay_byte_identic"


@pytest.mark.parametrize("status", [500, 503])
def test_unready_instance_stops_before_sending_traffic(monkeypatch, status):
    """Dacă `/health/ready` nu e 200, nu are rost să trimitem un tur (și să-l plătim)."""
    seen = _wire(monkeypatch, [("/health/ready", "GET", (status, {}, "{}"))])

    report = smoke.run_smoke(BASE, TOKEN)

    assert report["ok"] is False
    assert report["failed_step"] == "health_ready"
    assert report["profile"] == "unknown"
    # Nu mai e „o singură cerere" (readiness se reîncearcă), dar invariantul care CONTA se
    # păstrează: nu s-a trimis niciun tur — deci nici nu s-a plătit unul.
    assert {url for _m, url in seen} == {f"{BASE}/health/ready"}
    assert len(seen) > 1  # a insistat, nu a renunțat la primul 503


# ── Poarta care lipsea: „a răspuns" vs „plasa de siguranță a răspuns" ─────────────────────────
# Incidentul din 24 aug 2026: promovarea lui `bbb77b3` a trecut verde în timp ce FIECARE tur de
# vânzare cădea pe `fallback_stage` (modelul agent refuza `temperature` cu 400). Smoke-ul n-avea
# cum să vadă: trimitea „salut", care iese pe `greeting_stage` fără niciun apel de model.


def test_mesajul_de_smoke_nu_e_un_salut():
    """Un salut e servit de un strat gratuit, determinist — deci nu exersează niciun model.
    Poarta trebuie să trimită o cerere care ajunge la agent, altfel măsoară doar că serverul e
    sus."""
    assert len(smoke.SMOKE_MESSAGE.split()) >= 4
    assert smoke.SMOKE_MESSAGE.strip().lower() not in {"salut", "buna", "bună", "hei", "hello"}


def test_markerul_de_fallback_e_chiar_textul_din_runner():
    """Textul e duplicat în smoke (rulează contra unui host remote, nu importă `src`). Garda asta
    e motivul pentru care duplicarea e acceptabilă: dacă cineva reformulează fallback-ul din
    runner, testul pică AICI, nu peste trei luni într-o promovare care trece verde degeaba."""
    from pathlib import Path

    runner_src = Path(smoke.ROOT, "src", "worker", "runner.py").read_text(encoding="utf-8")
    assert smoke.RUNNER_FALLBACK_MARKER in runner_src


def test_fallbackul_de_runner_pica_smokeul_pe_v1(monkeypatch):
    raw = json.dumps(
        {
            "content": "Hmm, n-am înțeles exact 🙂 Cauți un produs anume, ai o întrebare "
            "despre o comandă, sau altceva?",
            "products": [],
        }
    )
    _wire(monkeypatch, _v1_routes((200, json.loads(raw), raw)))

    report = smoke.run_smoke(BASE, TOKEN)

    # 200, text nevid, politicos — și totuși produsul e mort. Exact ce trebuie prins.
    assert report["ok"] is False
    assert report["failed_step"] == "raspuns_v1_nu_e_fallback_de_runner"


def test_fallbackul_de_runner_pica_smokeul_si_pe_v2(monkeypatch):
    terminal = json.dumps(
        {
            "turn": {"id": "t-1", "status": "completed"},
            "view": {"blocks": [{"type": "text", "text": "Hmm, n-am înțeles exact 🙂 Cauți..."}]},
        }
    )
    _wire(
        monkeypatch,
        [
            ("/health/ready", "GET", HEALTH),
            ("/web/bootstrap", "GET", BOOTSTRAP),
            ("/web/v2/turns?", "POST", (202, {"turn": {"id": "t-1"}}, "{}")),
            ("/web/v2/turns/t-1", "GET", (200, json.loads(terminal), terminal)),
        ],
    )

    report = smoke.run_smoke(BASE, TOKEN)

    assert report["ok"] is False
    assert report["failed_step"] == "raspuns_nu_e_fallback_de_runner"


def test_markerul_se_gaseste_si_cand_diacriticele_sunt_escapate():
    """Un `ensure_ascii=True` oriunde pe drum ar transforma diacriticele în escape-uri unicode.
    O gardă care nu mai potrivește nimic raportează verde — cel mai prost mod de a eșua."""
    escaped = json.dumps({"content": "Hmm, n-am înțeles exact 🙂"}, ensure_ascii=True)
    assert "n-am înțeles" not in escaped  # chiar e escapat, testul verifică ce crede că verifică
    assert smoke._is_runner_fallback(escaped)


def test_un_503_de_dupa_deploy_nu_mai_pica_releaseul(monkeypatch):
    """Regresia din promovarea lui `6a74cf1` (2026-08-24): deployul a reușit, dar smoke-ul a lovit
    `/health/ready` la 2s după el și a primit `503` de la Traefik, care încă nu re-înregistrase
    backendul. Releaseul a ieșit roșu peste o promovare bună, iar „Înregistrează championul" a fost
    sărit — deci ținta de rollback a buildului următor a rămas nescrisă.

    `deploy.sh` așteaptă healthcheck-ul CONTAINERULUI; smoke-ul intră prin EDGE. Sunt două porți,
    iar a doua n-o aștepta nimeni."""
    raw = json.dumps(
        {"content": "Pentru ten gras îți recomand serul X.", "products": [{"id": "p1"}]}
    )
    ready = iter([(503, {}, "{}"), (503, {}, "{}"), HEALTH])

    def fake_request(url, *, method="GET", body=None, timeout=15.0):
        if "/health/ready" in url:
            return next(ready)
        if "/web/bootstrap" in url:
            return BOOTSTRAP
        if "/web/v2/turns" in url:
            return (404, {"detail": "not found"}, '{"detail":"not found"}')
        return (200, json.loads(raw), raw)

    monkeypatch.setattr(smoke, "_request", fake_request)

    report = smoke.run_smoke(BASE, TOKEN)

    assert report["ok"] is True
    health = next(s for s in report["steps"] if s["step"] == "health_ready")
    assert health["attempts"] == 3  # a insistat până a intrat în rotație
    assert "waited_s" in health  # cât a durat încălzirea ajunge în artefact, nu se pierde


def test_o_pana_reala_tot_pica_dupa_ce_expira_asteptarea(monkeypatch):
    """Așteptarea e MĂRGINITĂ: o instanță care nu-și revine rămâne un eșec, nu o buclă."""
    seen = _wire(monkeypatch, [("/health/ready", "GET", (503, {}, "{}"))])

    report = smoke.run_smoke(BASE, TOKEN)

    assert report["ok"] is False
    assert report["failed_step"] == "health_ready"
    assert {url for _m, url in seen} == {f"{BASE}/health/ready"}
