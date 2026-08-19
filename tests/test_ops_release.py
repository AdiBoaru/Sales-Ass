"""NX-248 — porțile STATICE de release: manifest, workflows, compose, imagine, lock.

Testele astea nu rulează un deploy; verifică proprietățile pe care un deploy se sprijină și care
se pierd tăcut la prima editare grăbită:

  • un `uses:` depin-uit la tag readuce cod mutabil în workflow-ul care are acces la GHCR;
  • un `latest` reintrodus în compose readuce „nu știm ce rulează";
  • `DATABASE_URL_MIGRATION` mutat în `env_file`-ul comun dă drept de DDL fiecărui proces;
  • un lock regenerat fără `--generate-hashes` transformă verificarea în decor.

Sunt teste de fișier, deliberat: proprietățile astea trăiesc în configurație, iar configurația nu
are teste unitare dacă nu i le scriem noi.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from dataclasses import fields
from pathlib import Path

import pytest
import yaml

from src.ops.manifest import MANIFEST_VERSION, DeployManifest, ManifestError, load

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / ".github" / "workflows"
COMPOSE_PROD = ROOT / "docker-compose.prod.yml"


def _manifest(**kw) -> DeployManifest:
    base = {
        "version": MANIFEST_VERSION,
        "image": "ghcr.io/adiboaru/sales-ass",
        "digest": "sha256:" + "a" * 64,
        "release_sha": "f8ce9c4",
        "built_at": "2026-08-17T10:00:00Z",
        "schema_requires": 42,
        "schema_tolerates": 43,
    }
    return DeployManifest(**{**base, **kw})


# ── Manifest ─────────────────────────────────────────────────────────────────────────────────


def test_manifestul_e_canonic_si_stabil():
    """Două serializări ale aceleiași stări = aceiași bytes. Fără asta, amprenta n-ar detecta
    o editare, ci ar semnala zgomot."""
    assert _manifest().fingerprint() == _manifest().fingerprint()
    assert _manifest().canonical() == _manifest().canonical()


def test_manifest_editat_e_respins():
    raw = _manifest().to_json()
    tampered = json.loads(raw)
    tampered["digest"] = "sha256:" + "b" * 64  # atacul evident: promovează altă imagine
    with pytest.raises(ManifestError, match="amprenta"):
        load(json.dumps(tampered))


def test_semnatura_ceruta_fara_cheie_e_respinsa():
    raw = _manifest().to_json("cheie-de-test")
    load(raw, key="cheie-de-test", require_signature=True)  # calea fericită
    with pytest.raises(ManifestError, match="RELEASE_MANIFEST_KEY"):
        load(raw, key=None, require_signature=True)
    with pytest.raises(ManifestError, match="semnătura"):
        load(raw, key="alta-cheie", require_signature=True)


def test_versiune_necunoscuta_de_manifest_e_respinsa():
    raw = _manifest().to_json()
    data = json.loads(raw)
    data["version"] = "deploy-manifest.v99"
    data.pop("fingerprint")
    # Recalculăm amprenta ca testul să izoleze verificarea de VERSIUNE, nu pe cea de conținut.
    forged = DeployManifest(**{k: v for k, v in data.items() if k != "signature"})
    with pytest.raises(ManifestError, match="versiune"):
        load(forged.to_json())


@pytest.mark.parametrize(
    ("applied", "prev_tolerates", "prev_digest", "possible"),
    [
        (42, 43, "sha256:" + "b" * 64, True),
        (43, 43, "sha256:" + "b" * 64, True),
        (44, 43, "sha256:" + "b" * 64, False),  # schema a trecut dincolo de imaginea veche
        (42, 43, "", False),  # primul release: nu există țintă
        (42, -1, "sha256:" + "b" * 64, False),  # interval necunoscut ⇒ nu promitem
    ],
)
def test_fezabilitatea_rollbackului(applied, prev_tolerates, prev_digest, possible):
    manifest = _manifest(previous_digest=prev_digest, previous_schema_tolerates=prev_tolerates)
    assert manifest.rollback_possible(applied)[0] is possible


def test_manifestul_nu_contine_secrete():
    """Manifestul ajunge în artefacte de CI păstrate un an. Câmpurile lui sunt, prin construcție,
    identificatori și numere — nu credentiale."""
    raw = _manifest().to_json("cheie-secreta-de-semnare")
    assert "cheie-secreta-de-semnare" not in raw, "cheia semnează, nu se publică"


# ── Workflows ────────────────────────────────────────────────────────────────────────────────


def _workflows() -> dict[str, dict]:
    return {p.name: yaml.safe_load(p.read_text(encoding="utf-8")) for p in WORKFLOWS.glob("*.yml")}


def _uses(node) -> list[str]:
    found: list[str] = []
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "uses" and isinstance(value, str):
                found.append(value)
            else:
                found.extend(_uses(value))
    elif isinstance(node, list):
        for item in node:
            found.extend(_uses(item))
    return found


_SHA_PIN = re.compile(r"^[^@]+@[0-9a-f]{40}$")


def test_toate_actiunile_sunt_pinuite_pe_sha():
    """Un tag e mutabil: `@v4` de mâine poate fi alt cod, care rulează cu accesul workflow-ului
    la GHCR și la secretele de deploy."""
    unpinned = [
        f"{name}: {uses}"
        for name, wf in _workflows().items()
        for uses in _uses(wf)
        if not _SHA_PIN.match(uses) and not uses.startswith("./")
    ]
    assert not unpinned, f"acțiuni ne-pinuite pe commit SHA: {unpinned}"


def test_permisiunile_sunt_minime_la_nivel_de_workflow():
    for name, wf in _workflows().items():
        assert "permissions" in wf, f"{name}: fără `permissions` explicit (moștenește tot)"
        assert wf["permissions"] in ({}, None, "read-all") or isinstance(wf["permissions"], dict), (
            f"{name}: permisiuni prea largi"
        )


def _triggers(workflow: dict) -> dict:
    """Cheia `on:` — pe care YAML 1.1 o parsează ca booleanul `True`, nu ca șirul „on"."""
    return workflow.get("on") or workflow.get(True) or {}


def test_deployul_vechi_nu_mai_e_automat():
    """Un merge în `main` nu mai este o aprobare de deploy în producție."""
    triggers = _triggers(_workflows()["deploy.yml"])
    assert "push" not in triggers, "calea deprecată nu are voie să pornească singură la push"
    assert "workflow_dispatch" in triggers


def test_releaseul_nu_promoveaza_in_productie_la_push():
    """Jobul de producție există DOAR cu un digest dat explicit — deci după o decizie umană."""
    production = _workflows()["release.yml"]["jobs"]["production"]
    assert "inputs.digest" in production["if"]
    assert production["environment"] == "production", "aprobarea trăiește în GitHub Environments"


def test_releaseul_verifica_semnatura_inainte_de_deploy():
    steps = json.dumps(_workflows()["release.yml"]["jobs"]["production"]["steps"])
    verify_at = steps.index("cosign verify")
    deploy_at = steps.index("deploy.sh")
    assert verify_at < deploy_at, "semnătura se verifică ÎNAINTE de a atinge hostul"


def test_scanul_de_vulnerabilitati_e_fail_closed():
    build = json.dumps(_workflows()["release.yml"]["jobs"]["build"]["steps"])
    assert "continue-on-error" not in build, "un scan care nu poate pica nu e o poartă"
    assert '"exit-code": "1"' in build or "'exit-code': '1'" in build


# ── Compose de producție ─────────────────────────────────────────────────────────────────────


def _compose() -> dict:
    return yaml.safe_load(COMPOSE_PROD.read_text(encoding="utf-8"))


#: Serviciile aplicației (aceeași imagine). `redis` are propriul regim.
APP_SERVICES = (
    "webhook",
    "worker",
    "dispatcher",
    "scheduler",
    "migrate",
    "telegram-poller",
    "proactive",
)


def test_compose_nu_mai_foloseste_latest():
    raw = COMPOSE_PROD.read_text(encoding="utf-8")
    assert ":latest" not in raw, "producția nu consumă taguri mutabile"
    assert "IMAGE_DIGEST" in raw


def test_compose_cere_digest_altfel_nu_porneste():
    """`:?` în interpolare = `docker compose config` eșuează fără digest. Un deploy care nu știe
    ce artefact pornește nu trebuie să pornească nimic."""
    raw = COMPOSE_PROD.read_text(encoding="utf-8")
    assert "${IMAGE_DIGEST:?" in raw


def test_toate_serviciile_de_app_sunt_intarite():
    compose = _compose()
    # Ancora YAML e deja rezolvată de parser, deci verificăm SERVICIUL, nu doar ancora: dacă
    # cineva scrie un serviciu nou fără `<<: *app`, testul îl prinde.
    for name in APP_SERVICES:
        service = compose["services"][name]
        assert service.get("read_only") is True, f"{name}: filesystem scriibil"
        assert service.get("cap_drop") == ["ALL"], f"{name}: capabilități nedropate"
        assert "no-new-privileges:true" in service.get("security_opt", []), f"{name}"
        assert service.get("pids_limit"), f"{name}: fără plafon de procese"
        assert service.get("tmpfs"), f"{name}: read_only fără tmpfs nu poate scrie heartbeat"


def test_credentialul_de_migrare_nu_ajunge_in_runtime():
    """Poarta care face ca „DDL doar din jobul de migrare" să fie o proprietate, nu o convenție."""
    compose = _compose()
    for name, service in compose["services"].items():
        env_files = service.get("env_file") or []
        env_files = [env_files] if isinstance(env_files, str) else env_files
        environment = json.dumps(service.get("environment") or {})
        if name == "migrate":
            assert ".env.migrate" in env_files, "jobul de migrare are nevoie de credentialul de DDL"
            continue
        assert ".env.migrate" not in env_files, f"{name}: primește credentialul de DDL"
        assert "DATABASE_URL_MIGRATION" not in environment, f"{name}: DDL în mediu"


def test_jobul_de_migrare_nu_e_un_serviciu_pornit_de_up():
    migrate = _compose()["services"]["migrate"]
    assert "migrate" in (migrate.get("profiles") or []), "ar porni la `docker compose up -d`"
    assert migrate.get("restart") == "no", "o migrare eșuată nu se reia în buclă"


def test_healthcheckurile_sunt_semantice_nu_pe_socket():
    compose = _compose()
    webhook = json.dumps(compose["services"]["webhook"]["healthcheck"])
    assert "/health/ready" in webhook, "socketul deschis nu mai e o dovadă de sănătate"
    assert "create_connection" not in webhook
    for name in ("worker", "scheduler"):
        check = json.dumps(compose["services"][name]["healthcheck"])
        assert "src.ops.worker_health" in check, f"{name}: fără health real"


def test_imaginile_externe_sunt_pinuite_pe_digest():
    """Imaginea NOASTRĂ vine prin `@${IMAGE_DIGEST}` (digest din manifest, validat la deploy);
    orice imagine terță trebuie să poarte un digest LITERAL. `redis:7-alpine` se mută — backbone-ul
    cozii nu are voie să se schimbe sub tine la un `pull` de rutină."""
    raw = COMPOSE_PROD.read_text(encoding="utf-8")
    for line in raw.splitlines():
        stripped = line.strip()
        if not stripped.startswith("image:"):
            continue
        pinned = "@sha256:" in stripped or "@${IMAGE_DIGEST" in stripped
        assert pinned, f"imagine ne-pinuită: {stripped}"


# ── Dockerfile, lock, dockerignore ───────────────────────────────────────────────────────────


def test_dockerfile_pinuieste_baza_pe_digest_si_cere_hashuri():
    text = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert re.search(r"FROM python@\$\{BASE_DIGEST\}", text), "baza nu e pin-uită pe digest"
    assert re.search(r"BASE_DIGEST=sha256:[0-9a-f]{64}", text)
    assert "--require-hashes" in text, "instalare fără verificare de hash"
    assert "USER 10001:10001" in text, "runtime ca root"


def test_lockul_are_hashuri_pentru_fiecare_pachet():
    """Un lock regenerat fără `--generate-hashes` ar trece de `pip install` și ar face
    `--require-hashes` inutil — verificarea ar rămâne în Dockerfile ca text."""
    lines = (ROOT / "requirements.lock").read_text(encoding="utf-8").splitlines()
    packages = [line for line in lines if re.match(r"^[a-z0-9][a-z0-9._-]*==", line)]
    assert len(packages) > 20, "lock suspect de mic"
    for package in packages:
        assert package.rstrip().endswith("\\"), f"pachet fără hash: {package}"
    assert sum(1 for line in lines if "--hash=sha256:" in line) >= len(packages)


def test_lockul_nu_mai_contine_uneltele_de_linie_de_comanda():
    """`fastapi[standard]` aducea un CLI, un CLI de cloud și un SDK de telemetrie către un terț
    într-un proces care trece tot ce iese prin sanitizarea NX-230/246."""
    lock = (ROOT / "requirements.lock").read_text(encoding="utf-8")
    for unwanted in ("sentry-sdk==", "fastapi-cli==", "fastapi-cloud-cli=="):
        assert unwanted not in lock, f"{unwanted} a revenit în imaginea de runtime"


def test_contextul_de_build_exclude_istoricul_git_si_secretele():
    ignore = (ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()
    entries = {line.strip() for line in ignore if line.strip() and not line.startswith("#")}
    assert ".git" in entries, ".git poartă ISTORICUL, deci și secretele comise și șterse ulterior"
    assert ".env" in entries and ".env.*" in entries
    # Registrul NX-173 trebuie să RĂMÂNĂ: excluderea lui a fost chiar bugul (poarta de boot cade).
    assert "!db/seed/safety_rules.json" in entries


def test_manifestul_nu_poarta_amprenta_de_config():
    """v2 a scos `config_revision` — și trebuie să RĂMÂNĂ scos.

    Era o amprentă a configurației, calculată la BUILD, în CI, unde `.env`-ul hostului nu există:
    ieșea amprenta default-urilor din cod. `verify_manifest` o compara cu ce raportează
    `/health/ready` de pe VPS, unde `.env` are cel puțin `ENV=development` față de `dev` în cod —
    deci nu puteau coincide niciodată, iar verificarea striga „deploy parțial" la fiecare deploy
    corect. Testul e aici ca nimeni să n-o pună la loc dintr-un reflex de „lipsește un câmp".
    """
    assert "config_revision" not in {f.name for f in fields(DeployManifest)}
    assert "config_revision" not in _manifest().to_json()


def test_versiunea_de_manifest_e_v2():
    """Schimbarea de formă e VIZIBILĂ: un manifest v1 de pe un host vechi trebuie respins la
    citire, nu interpretat pe jumătate."""
    assert MANIFEST_VERSION == "deploy-manifest.v2"
    with pytest.raises(ManifestError, match="versiune de manifest necunoscută"):
        load(_manifest().to_json().replace(MANIFEST_VERSION, "deploy-manifest.v1"))


def test_build_manifest_nu_are_nevoie_de_dependente_instalate():
    """`build_manifest.py` rulează în jobul de BUILD, care nu instalează `requirements*.txt`.

    Regresie măsurată (2026-08-19): scriptul importa `src.config` pentru `Settings()`, iar pasul
    cădea cu `ModuleNotFoundError: No module named 'pydantic'` — la ultimul pas din cinci, deci
    fără manifest, fără staging, fără nimic de promovat. Defectul stătuse ascuns fiindcă jobul
    murea mai devreme, la scan.

    Verificăm în SUBPROCES, cu importurile izolate: în sesiunea de pytest `pydantic` e deja
    încărcat de alte module, deci un assert pe `sys.modules` din interior n-ar dovedi nimic.
    """
    code = (
        "import sys, importlib;"
        "sys.path.insert(0, r'%s');"
        "importlib.import_module('scripts.release.build_manifest');"
        "print('pydantic' in sys.modules)"
    ) % str(ROOT)
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=False)
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "False", (
        "build_manifest.py trage pydantic în proces — jobul de build n-are dependențe instalate"
    )


# ── Preflight: primul release nu poate fi un impas ──────────────────────────────────────────


def _preflight():
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "preflight", ROOT / "scripts" / "release" / "preflight.py"
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules["preflight"] = mod
    spec.loader.exec_module(mod)
    return mod


def test_primul_release_e_o_stare_nu_un_esec():
    """`--require-rollback-possible` cere un predecesor; la PRIMUL release nu există niciunul.

    Poarta era imposibil de trecut prin construcție — bootstrap blocat. Ieșit la iveală
    2026-08-19, la prima promovare care a ajuns până acolo. Acceptarea e DECLARATĂ
    (`--allow-no-previous-digest`), nu dedusă: din manifest, „primul release" și „manifestul
    precedent n-a putut fi citit" arată identic — `build_manifest.py` lasă `previous_digest` gol
    în ambele cazuri, iar a doua e exact situația în care vrei să te oprești.
    """
    m = _manifest(previous_digest="", previous_schema_tolerates=-1)
    possible, why = m.rollback_possible(applied_schema=44)
    assert possible is False
    assert "primul release" in why


def test_acceptarea_nu_relaxeaza_si_schema_depasita():
    """Steagul acoperă DOAR absența unei ținte. O schemă care depășește ce tolerează imaginea
    precedentă rămâne blocantă — acolo rollbackul chiar ar rula cod orb peste coloane noi."""
    m = _manifest(previous_digest="sha256:" + "b" * 64, previous_schema_tolerates=43)
    possible, why = m.rollback_possible(applied_schema=44)
    assert possible is False
    assert "depășește" in why
    assert m.previous_digest, "cazul ăsta NU e primul release, deci steagul nu se aplică"


def test_preflight_expune_steagul_de_prim_release():
    """Contract de CLI: numele steagului e citat în runbook și în workflow. Dacă se redenumește,
    promovarea pică în CI cu „unrecognized arguments", nu aici — de aceea îl fixăm în test."""
    mod = _preflight()
    parser_args = mod.main.__doc__ or ""
    src = (ROOT / "scripts" / "release" / "preflight.py").read_text(encoding="utf-8")
    assert '"--allow-no-previous-digest"' in src, parser_args
