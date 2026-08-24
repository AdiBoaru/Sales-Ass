"""NX-248 — smoke WebWidget: verifică CONTRACTUL PE CARE ÎL SERVEȘTE INSTANȚA, nu unul dorit.

Un smoke care verifică „200 OK" nu verifică produsul. Dar un smoke care cere un contract STINS e
și mai rău: pică după ce deployul a schimbat deja imaginea, deci raportează „release picat" pentru
un release care a reușit — iar operatorul dă un rollback de care nu era nevoie.

Exact asta s-ar fi întâmplat aici. Scriptul cerea `POST /web/v2/turns` → 202, dar v2 e în spatele
lui `WEB_TURN_V2_ENABLED`, stins în producție prin decizie ratificată (NX-249 e BLOCAT, NX-247
NO-GO, NX-238 NOT-READY). Măsurat pe producție 2026-08-19: ruta răspunde 404 cu parametri și 422
fără ei — adică ruta EXISTĂ, flagul e stins. Un gate pe care nimeni nu-l poate trece nu filtrează
nimic; e aceeași greșeală reparată la poarta de scan din `release.yml`, cu altă față.

Deci profilul se DETECTEAZĂ, nu se presupune, și ajunge în raport:

  * `v2` — calea asincronă (NX-232/233) e pornită: accept durabil → terminal → replay byte-identic
    → idempotență. Replay-ul e cel care prinde regresiile scumpe: dacă a doua citire diferă,
    „refresh-ul paginii" schimbă răspunsul deja dat clientului.
  * `v1` — calea sincronă (`POST /web/chat`), cea pe care o folosesc clienții ACUM. Un tur real,
    dus până la răspuns. Replay/idempotență nu se verifică: sunt garanții pe care v1 nu le promite,
    iar un smoke nu are voie să inventeze contracte.

Trecerea de la v1 la v2 se face singură la cutover, fără să atingă nimeni fișierul ăsta. Ca să nu
poată REGRESA tăcut înapoi pe v1 după cutover, `--expect-profile v2` (sau `SMOKE_EXPECT_PROFILE`)
transformă căderea pe v1 în eșec.

Verifică, în ordine:

  1. `/health/ready` — 200 (altfel nu are rost să trimitem trafic);
  2. `/web/bootstrap` — sesiune emisă;
  3. detectarea profilului (un singur accept, nu două cereri);
  4. lanțul profilului detectat, până la un răspuns REAL;
  5. că răspunsul NU e fallback-ul de runner (vezi `RUNNER_FALLBACK_MARKER`).

Pasul 5 e cel care face din 4 o verificare adevărată: pipeline-ul are o plasă care răspunde
politicos când niciun stagiu n-a produs nimic, deci „200 cu text nevid" e compatibil cu un creier
de vânzare complet mort. Pe 24 aug 2026 exact asta s-a întâmplat.

## Ce NU ajunge în artefact

Nici textul răspunsului, nici mesajul trimis, nici tokenul, nici `visitor_id`-ul. Raportul poartă
verdicte, durate, lungimi și amprente (SHA-256 trunchiat) — suficient ca să demonstreze
determinismul, insuficient ca să scurgă conversația unui client într-un artefact de CI care se
păstrează 90 de zile. Turul se rulează pe un tenant de TEST dedicat (`SMOKE_PUBLIC_TOKEN`), nu pe
al unui client real.

Uz: SMOKE_BASE_URL=https://… SMOKE_PUBLIC_TOKEN=… python scripts/release/smoke_web_v2.py --json
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

EXIT_OK = 0
EXIT_FAIL = 1
EXIT_ERROR = 2

#: Mesajul de smoke. Fără date personale (ajunge în conversația tenantului de test și în logurile
#: lui), dar DELIBERAT o cerere de produs, nu un salut.
#:
#: A fost „salut" până pe 24 aug 2026, și exact asta a lăsat să treacă verde promovarea lui
#: `bbb77b3`: un salut iese pe `greeting_stage` (strat gratuit, determinist), deci smoke-ul nu
#: atingea NICIUN model. În buildul promovat, fiecare tur de vânzare pica pe un `400` de la
#: furnizor, iar poarta n-avea cum să vadă — dovedea că aplicația e sus, nu că vinde.
#:
#: O cerere de produs trece prin tot lanțul: alias → cache → FAQ (toate „miss" pe o cerere reală)
#: → triaj → agent → tool-uri → validator → randare.
SMOKE_MESSAGE = "caut un produs pentru ten gras, ce îmi recomanzi?"

#: Fragment din fallback-ul de runner (`fallback_stage`, src/worker/runner.py) — plasa care se
#: aprinde când NICIUN stagiu n-a produs reply. E un 200 cu text valid, deci nicio verificare de
#: status sau de „nu e gol" nu-l poate distinge de un răspuns adevărat: singura diferență e CE
#: scrie. Duplicarea textului aici e intenționată (smoke-ul rulează contra unui host remote și nu
#: importă `src`), iar `tests/test_release_smoke.py` ține cele două șiruri sincronizate.
RUNNER_FALLBACK_MARKER = "n-am înțeles exact"


class SmokeError(RuntimeError):
    pass


def _request(url: str, *, method: str = "GET", body: dict | None = None, timeout: float = 15.0):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(  # noqa: S310 — URL de operator, nu input de utilizator
        url, data=data, method=method, headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:  # noqa: S310
            raw = response.read().decode("utf-8")
            return response.status, (json.loads(raw) if raw else {}), raw
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        try:
            return e.code, json.loads(raw), raw
        except ValueError:
            return e.code, {}, raw


def _digest(raw: str) -> str:
    """Amprentă a răspunsului, nu răspunsul: demonstrează egalitatea fără să publice conținutul."""
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _is_runner_fallback(raw: str) -> bool:
    """`raw` conține fallback-ul de runner? Caută AMBELE forme, fiindcă nu putem presupune cum
    serializează serverul diacriticele: Starlette scrie UTF-8 literal, dar un `ensure_ascii=True`
    oriunde pe drum ar transforma marker-ul în `\\u0163`-uri și verificarea ar deveni tăcut
    inutilă — exact tipul de gardă care raportează verde fiindcă nu mai potrivește nimic."""
    escaped = json.dumps(RUNNER_FALLBACK_MARKER, ensure_ascii=True)[1:-1]  # fără ghilimele
    return RUNNER_FALLBACK_MARKER in raw or escaped in raw


def run_smoke(
    base: str, token: str, *, deadline_s: float = 60.0, expect_profile: str = "auto"
) -> dict:
    """Rulează lanțul și întoarce raportul. Nu ridică: un smoke picat trebuie să lase în urmă
    exact pașii care AU trecut — altfel „a picat" nu spune unde, iar diagnosticul reîncepe de la
    zero pe un mediu pe care poate nu-l mai poți reproduce."""
    # `profile` pornește necunoscut și rămâne așa dacă lanțul moare înainte de detectare. Un raport
    # verde fără profil ar fi de necitit: „a trecut" — dar CE a trecut?
    report: dict = {"steps": [], "ok": False, "profile": "unknown"}

    def step(name: str, ok: bool, **extra) -> None:
        report["steps"].append({"step": name, "ok": ok, **extra})
        if not ok:
            raise SmokeError(name)

    started = time.monotonic()
    try:
        _run_steps(base, token, step, started, deadline_s, expect_profile, report)
    except SmokeError as e:
        report["failed_step"] = str(e)
        return report
    except Exception as e:  # noqa: BLE001 — tipul, nu mesajul (poate purta URL/credential)
        report["error"] = type(e).__name__
        return report
    report["ok"] = True
    report["duration_s"] = round(time.monotonic() - started, 2)
    return report


def _run_steps(
    base: str,
    token: str,
    step,
    started: float,
    deadline_s: float,
    expect_profile: str,
    report: dict,
) -> None:
    # 1. Readiness
    status, payload, _ = _request(f"{base}/health/ready")
    step("health_ready", status == 200, status=status, release=payload.get("release", "unknown"))

    # 2. Bootstrap
    status, session, _ = _request(f"{base}/web/bootstrap?token={urllib.parse.quote(token)}")
    step("bootstrap", status == 200 and bool(session.get("visitor_id")), status=status)

    auth = urllib.parse.urlencode(
        {"token": token, "visitor_id": session["visitor_id"], "sig": session["sig"]}
    )
    client_turn_id = str(uuid.uuid4())

    # 3. Accept durabil — și, în același request, DETECTAREA profilului.
    #
    # Nu se face o cerere separată de „probing": ar fi două tururi pornite pe tenantul de test la
    # fiecare deploy, dintre care unul aruncat. Acceptul E sonda.
    status, accepted, _ = _request(
        f"{base}/web/v2/turns?{auth}",
        method="POST",
        body={
            "schema_version": "web-turn.v2",
            "client_turn_id": client_turn_id,
            "input": {"type": "text", "text": SMOKE_MESSAGE},
        },
    )

    # 404 aici nu e ambiguu: `_v2_gate` verifică flagul ÎNAINTEA sesiunii, deci un 404 pe o cerere
    # cu sesiune validă înseamnă „feature stins", nu „ruta lipsește" (ruta lipsă ar da 404 și fără
    # parametri, unde FastAPI răspunde 422 fiindcă validează query-ul înaintea handlerului).
    if status == 404:
        report["profile"] = "v1"
        step(
            "profil_detectat",
            expect_profile != "v2",
            profile="v1",
            expected=expect_profile,
            # Mesajul e pentru cine citește artefactul peste trei luni, nu pentru acum.
            note="v2 stins (WEB_TURN_V2_ENABLED); verific v1 sincron, calea servită clienților",
        )
        _run_v1_chain(base, token, session, step)
        return

    report["profile"] = "v2"
    step("profil_detectat", True, profile="v2", expected=expect_profile)

    turn_id = (accepted.get("turn") or {}).get("id") or accepted.get("turn_id")
    step("accept", status == 202 and bool(turn_id), status=status)

    # 4. Poll până la terminal, MĂRGINIT.
    terminal_raw, terminal_status = None, None
    while time.monotonic() - started < deadline_s:
        status, payload, raw = _request(f"{base}/web/v2/turns/{turn_id}?{auth}")
        if status == 200:
            terminal_raw, terminal_status = raw, payload.get("turn", {}).get("status")
            break
        if status != 202:
            step("poll", False, status=status)
        time.sleep(max(0.5, (payload.get("poll_after_ms") or 800) / 1000.0))
    step(
        "terminal",
        terminal_raw is not None,
        status=terminal_status,
        elapsed_s=round(time.monotonic() - started, 2),
        # Lungimea, nu conținutul: un rezultat terminal GOL e un bug (P6: niciodată tăcere), iar
        # asta se poate afirma fără să publicăm răspunsul.
        payload_bytes=len(terminal_raw or ""),
    )
    step("terminal_nu_e_gol", len(terminal_raw or "") > 2)
    # „Nevid" nu e „a răspuns": fallback-ul de runner e tot text valid, într-un 200. Pe o cerere
    # de produs, plasa aprinsă înseamnă că agentul a murit (model refuzat, tool loop căzut) și că
    # restul sistemului pare sănătos exact fiindcă triajul de deasupra a mers.
    step("raspuns_nu_e_fallback_de_runner", not _is_runner_fallback(terminal_raw or ""))

    # 5. Replay: a doua citire, aceiași bytes.
    _status, _payload, replay_raw = _request(f"{base}/web/v2/turns/{turn_id}?{auth}")
    identical = replay_raw == terminal_raw
    step(
        "replay_byte_identic",
        identical,
        first=_digest(terminal_raw or ""),
        second=_digest(replay_raw or ""),
    )

    # 6. Idempotență: același client_turn_id nu creează alt tur.
    status, again, _ = _request(
        f"{base}/web/v2/turns?{auth}",
        method="POST",
        body={
            "schema_version": "web-turn.v2",
            "client_turn_id": client_turn_id,
            "input": {"type": "text", "text": SMOKE_MESSAGE},
        },
    )
    same_turn = ((again.get("turn") or {}).get("id") or again.get("turn_id")) == turn_id
    step("idempotenta", status in (200, 202) and same_turn, status=status)


def _run_v1_chain(base: str, token: str, session: dict, step) -> None:
    """Calea SINCRONĂ (`POST /web/chat`) — contractul pe care îl folosesc clienții azi.

    Aici răspunsul HTTP E transportul: un singur request duce turul prin tot pipeline-ul (gates,
    straturi gratuite, triaj, agent, validator) și întoarce `{content, products, suggestions}`.
    Deci „am primit `content` nevid" nu e o formalitate — e dovada că lanțul întreg trăiește:
    sesiune → tenant → DB tenant-scoped → model → validator → randare.

    Ce NU se verifică aici, deliberat: replay byte-identic și idempotența pe `client_msg_id`. Sunt
    garanții pe care le introduce ledgerul din v2 (NX-232); v1 nu le promite. Un smoke care le-ar
    cere oricum ar raporta ca defect exact comportamentul specificat.
    """
    status, payload, raw = _request(
        f"{base}/web/chat",
        method="POST",
        body={
            "token": token,
            "visitor_id": session["visitor_id"],
            "sig": session["sig"],
            "message": SMOKE_MESSAGE,
            "client_msg_id": str(uuid.uuid4()),
        },
        # Turul sincron rulează pipeline-ul IN-PROCES (DB + model), deci e mai lent decât un accept.
        timeout=45.0,
    )
    step("chat_v1", status == 200, status=status)
    content = payload.get("content") or ""
    # Lungimea și amprenta, nu textul: raportul se păstrează 365 de zile ca artefact de CI, iar
    # răspunsul e conversație de tenant. P6 spune „niciodată tăcere" — un `content` gol e un bug.
    step(
        "raspuns_v1_nu_e_gol",
        len(content.strip()) > 0,
        content_chars=len(content),
        products=len(payload.get("products") or []),
        digest=_digest(raw),
    )
    # Vezi nota din lanțul v2: „nevid" nu distinge un răspuns de plasa de siguranță.
    step("raspuns_v1_nu_e_fallback_de_runner", not _is_runner_fallback(content))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Smoke WebWidget v2 (NX-248)")
    ap.add_argument("--base-url", default=os.environ.get("SMOKE_BASE_URL", ""))
    ap.add_argument("--token", default=os.environ.get("SMOKE_PUBLIC_TOKEN", ""))
    ap.add_argument("--deadline", type=float, default=60.0)
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--out", default="")
    # `auto` = verifică ce servește instanța. După cutoverul NX-249, pune `v2` în workflow: atunci
    # o cădere înapoi pe v1 (flag stins din greșeală la un rollback de config) devine EȘEC, nu un
    # smoke verde care verifică tăcut alt contract decât cel promovat.
    ap.add_argument(
        "--expect-profile",
        choices=("auto", "v1", "v2"),
        default=os.environ.get("SMOKE_EXPECT_PROFILE", "auto"),
    )
    args = ap.parse_args(argv)

    if not args.base_url or not args.token:
        print("SMOKE_BASE_URL și SMOKE_PUBLIC_TOKEN sunt obligatorii", file=sys.stderr)
        return EXIT_ERROR

    report = run_smoke(
        args.base_url.rstrip("/"),
        args.token,
        deadline_s=args.deadline,
        expect_profile=args.expect_profile,
    )
    code = EXIT_OK if report.get("ok") else (EXIT_ERROR if report.get("error") else EXIT_FAIL)

    text = json.dumps(report, indent=2, sort_keys=True, ensure_ascii=False)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(text + "\n")
    if args.json or not args.out:
        print(text)
    # Profilul intră în linia de rezumat: în logul rulării se vede din prima CE contract a trecut,
    # fără să deschidă nimeni artefactul.
    verdict = "PASS" if report.get("ok") else "FAIL"
    print(f"SMOKE: {verdict} (profil={report.get('profile', 'unknown')})", file=sys.stderr)
    return code


if __name__ == "__main__":
    from src.ops.cli import enable_utf8_stdout

    enable_utf8_stdout()

    raise SystemExit(main())
