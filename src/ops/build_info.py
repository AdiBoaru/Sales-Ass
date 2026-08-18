"""NX-248 — identitatea artefactului care rulează: release, digest, config, interval de schemă.

Fără fișierul ăsta, întrebarea „ce rulează în producție?" se răspunde citind un tag mutabil.
`latest` de acum e alt bytes decât `latest` de ieri, deci un rollback „la latest" nu e o operație,
e o speranță. Aici artefactul își declară identitatea o singură dată, iar releaseul o VERIFICĂ
din afară.

## Ce poate și ce NU poate ști un container despre sine

Un proces își poate citi propriul SHA de sursă (îl coacem la build ca `RELEASE_SHA`), dar **nu-și
poate citi propriul digest de imagine**: digestul e amprenta manifestului care CONȚINE stratul în
care ar trebui scris, deci a-l coace înăuntru e o recursie imposibilă. De aceea `image_digest` vine
din mediu (îl pune deployul din manifest), iar adevărul se stabilește din AFARĂ: `docker inspect`
pe host compară digestul real cu cel din manifest (`scripts/release/verify_manifest.py`). Container
care se auto-declară e o afirmație; host care compară e o dovadă. Nu le confundăm: câmpul se
cheamă `image_digest_claimed`.

## Intervalul de schemă e mecanismul, nu o notă în runbook

Expand/contract funcționează doar dacă „imaginea precedentă merge pe schema nouă" e o proprietate
VERIFICABILĂ înainte de deploy, nu o promisiune. Imaginea declară două numere:

  • `schema_requires` — DERIVAT din migrările pe care le CONȚINE (maximul de pe disc). Nu poate
    deriva de la ce s-a livrat, fiindcă e citit din exact fișierele livrate.
  • `schema_tolerates` — `schema_requires + SCHEMA_FORWARD_TOLERANCE`, o declarație EXPLICITĂ:
    „tolerez atâtea migrări expand peste mine". Peste el, readiness dă 503 în loc să ruleze pe o
    schemă pe care n-a văzut-o nimeni.

Rollbackul devine astfel o întrebare cu răspuns: digestul precedent tolerează schema curentă?
Dacă nu, releaseul SE BLOCHEAZĂ înainte de deploy (cardul: „rollbackul este declarat imposibil
înainte de deploy"), fiindcă alternativa e să descoperi asta în incident.

## Revizia de config

`config_revision` e o amprentă peste COMPORTAMENTUL configurat (flag-uri, praguri, plafoane), nu
peste secrete: secretele nu descriu comportamentul și n-au ce căuta într-un identificator care
ajunge în loguri, metrici și manifest. Două deployuri cu aceeași revizie se comportă la fel; două
revizii diferite explică de ce un tur a mers altfel — fără să spună nimănui care e cheia.
"""

from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path
from typing import Any

#: Câte migrări EXPAND peste ce conține imaginea acceptă procesul înainte să refuze traficul.
#: 1 nu e o constantă magică, e politica de release: la un moment dat există cel mult DOUĂ imagini
#: valide (champion + candidate), deci cel mult o migrare între ele. Mai mult ar însemna că
#: rollbackul sare peste o migrare — exact cazul pe care cardul îl cere blocat înainte de deploy.
SCHEMA_FORWARD_TOLERANCE = 1

#: Locul unde stau migrările în imagine (Dockerfile copiază `docs/*.sql`).
_DOCS_DIR = Path(__file__).resolve().parent.parent.parent / "docs"
_MIGRATION_RE = re.compile(r"^(\d+)_.*\.sql$")

#: Forma unui SHA de git (scurt sau complet). Orice altceva e „unknown": preferăm să nu știm
#: decât să publicăm text liber într-un câmp pe care îl citesc dashboardurile.
_SHA_RE = re.compile(r"^[0-9a-f]{7,40}$")
#: Forma unui digest OCI. Nu acceptăm tag-uri aici — un tag în câmpul de digest e chiar bugul.
_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")

UNKNOWN = "unknown"


def _env(name: str) -> str:
    return (os.environ.get(name) or "").strip()


def _sanitized(value: str, pattern: re.Pattern[str]) -> str:
    return value if pattern.match(value) else UNKNOWN


def bundled_schema_version(docs_dir: Path | None = None) -> int:
    """Cea mai mare migrare PREZENTĂ în artefact (0 dacă nu e niciuna).

    Derivat, nu declarat: dacă cineva adaugă `043_*.sql` și uită să atingă un număr de undeva,
    imaginea își cere singură migrarea. O constantă scrisă de mână ar fi mințit exact în ziua în
    care conta.
    """
    directory = docs_dir or _DOCS_DIR
    best = 0
    if not directory.is_dir():
        return 0
    for path in directory.glob("*.sql"):
        m = _MIGRATION_RE.match(path.name)
        if m:
            best = max(best, int(m.group(1)))
    return best


@dataclass(frozen=True, slots=True)
class BuildInfo:
    """Cine e artefactul ăsta. Imutabil: identitatea nu se recalculează la runtime."""

    service: str
    role: str
    env: str
    release_sha: str
    release_track: str
    image_digest_claimed: str
    built_at: str
    config_revision: str
    schema_requires: int
    schema_tolerates: int

    def tolerates_schema(self, applied: int) -> bool:
        return self.schema_requires <= applied <= self.schema_tolerates

    def public(self) -> dict[str, Any]:
        """Ce are voie să vadă oricine atinge `/health/*`.

        Fără hostname, fără digest, fără nume de tenant: un răspuns de health e cel mai
        neautentificat endpoint din sistem, deci e și cel mai bun loc de recon. Rămâne exact cât
        trebuie ca un operator să spună „da, ăsta e releaseul pe care l-am promovat".
        """
        return {
            "service": self.service,
            "release": self.release_sha,
            "track": self.release_track,
            "config": self.config_revision,
            "schema": {"requires": self.schema_requires, "tolerates": self.schema_tolerates},
        }

    def operator(self) -> dict[str, Any]:
        """Vederea de operator (cale autorizată): identitatea completă, tot fără secrete."""
        return {
            **self.public(),
            "role": self.role,
            "env": self.env,
            "image_digest_claimed": self.image_digest_claimed,
            "built_at": self.built_at,
        }


#: Cuvintele care fac dintr-un câmp un secret. Comparate pe CUVINTE (split pe `_`), nu ca
#: substring, fiindcă substringul greșește în ambele direcții: `token` ar prinde
#: `llm_max_tokens_agent` (un plafon, nu un secret) și l-ar scoate TĂCUT din amprenta de config —
#: adică o schimbare reală de comportament ar deveni invizibilă în manifest.
_SECRET_WORDS = frozenset({"secret", "password", "token", "credential", "dsn", "key", "keys"})
#: Cuvintele care fac dintr-un `*_url` un DSN (conține parola), nu o adresă publică.
#: `checkout_base_url` ajunge în linkuri trimise clienților — el TREBUIE să rămână în amprentă.
_DSN_WORDS = frozenset({"db", "database", "redis"})
#: Câmpuri care conțin un cuvânt-marker dar NU sunt secrete (allowlist explicit, cu motiv).
_SECRET_EXEMPT = frozenset(
    {
        "web_session_secret_ttl_s",  # TTL-ul cache-ului de secret, nu secretul
        "web_action_key_id",  # identificatorul cheii, nu materialul ei
    }
)


def is_secret_field(name: str) -> bool:
    """Numele ăsta de setare ține un secret?

    Poartă pe NUME, nu pe valoare: o valoare goală azi (dev) e o cheie mâine (prod), iar un
    clasificator care se uită la valoare ar declara sigur exact câmpul pe care cineva uită să-l
    completeze în prod. Folosit de amprenta de config ȘI de redactarea din erori/health.

    `tests/test_ops_build_and_secrets.py` enumeră TOATE câmpurile din `Settings` și fixează
    verdictul fiecăruia: un câmp nou care arată a secret nu poate intra fără o decizie explicită.
    """
    if name in _SECRET_EXEMPT:
        return False
    words = set(name.lower().split("_"))
    if words & _SECRET_WORDS:
        return True
    return "url" in words and bool(words & _DSN_WORDS)


def config_revision(settings: Any) -> str:
    """Amprentă stabilă peste COMPORTAMENTUL configurat (12 hex).

    Include: flag-uri, praguri, plafoane, nume de model, versiuni de politici. Exclude: orice
    câmp clasificat secret și orice câmp care nu e scalar (o listă/dict ar aduce ordine
    nedeterministă în amprentă, adică o revizie care se schimbă fără ca nimic să se schimbe).

    Determinist prin construcție: sortăm cheile, serializăm valorile canonic. Aceeași config pe
    două hosturi ⇒ același `config_revision`. Asta e chiar proprietatea pe care se sprijină
    rollbackul: „config revision" din manifest e comparabilă, nu doar arhivabilă.
    """
    try:
        fields = sorted(type(settings).model_fields)
    except AttributeError:  # nu e un BaseSettings (teste cu dublă simplă)
        fields = sorted(k for k in vars(settings) if not k.startswith("_"))
    parts: list[str] = []
    for name in fields:
        if is_secret_field(name):
            continue
        value = getattr(settings, name, None)
        if isinstance(value, bool):
            parts.append(f"{name}={'1' if value else '0'}")
        elif isinstance(value, (int, float, str)):
            parts.append(f"{name}={value}")
        # restul (liste, dict-uri, obiecte) rămân în afara amprentei — vezi docstring.
    digest = hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()
    return digest[:12]


def build_info(settings: Any, *, role: str, docs_dir: Path | None = None) -> BuildInfo:
    """Compune identitatea artefactului din config + mediu + migrările prezente pe disc."""
    requires = bundled_schema_version(docs_dir)
    return BuildInfo(
        service=getattr(settings, "service_name", "nativx-assistant"),
        role=role,
        env=getattr(settings, "env", "dev"),
        release_sha=_sanitized(
            (getattr(settings, "release_sha", "") or _env("RELEASE_SHA")).lower(), _SHA_RE
        ),
        release_track=getattr(settings, "release_track", "champion"),
        image_digest_claimed=_sanitized(_env("IMAGE_DIGEST").lower(), _DIGEST_RE),
        built_at=_built_at(),
        config_revision=config_revision(settings),
        schema_requires=requires,
        schema_tolerates=requires + SCHEMA_FORWARD_TOLERANCE,
    )


def _built_at() -> str:
    """`BUILT_AT` din mediu dacă e un ISO-8601 UTC plauzibil, altfel `unknown`.

    Nu punem `now()` ca fallback: un „built_at" egal cu ora pornirii ar face fiecare restart să
    arate ca un build nou, adică exact minciuna pe care manifestul trebuie s-o excludă.
    """
    raw = _env("BUILT_AT")
    if not raw:
        return UNKNOWN
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return UNKNOWN
    return parsed.astimezone(UTC).isoformat(timespec="seconds")


@lru_cache(maxsize=8)
def cached_build_info(role: str) -> BuildInfo:
    """Identitatea procesului, calculată o singură dată (per rol). Se golește în teste cu
    `cached_build_info.cache_clear()`."""
    from src.config import get_settings  # noqa: PLC0415 — ciclu de import la încărcare

    return build_info(get_settings(), role=role)
