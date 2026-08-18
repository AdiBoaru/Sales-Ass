"""NX-249 — storeul de policy: citire validată, cache mărginit, CAS auditat, fail-closed.

Un release controller are exact două momente în care poate minți: când citește policy-ul (și
crede ceva ce nu e adevărat) și când îl scrie (și suprascrie tăcut decizia altcuiva). Modulul
ăsta le închide pe amândouă.

## Citirea

`current(...)` întoarce mereu un `PolicyView` — niciodată o excepție pe calea fierbinte. Un store
căzut, un tabel absent, un document invalid sau un mediu greșit produc `available=False` /
`policy=None` cu un **cod fix**, iar `assignment.resolve` transformă orice astfel de stare în
`control`. Motivele sunt distincte (`store_down` ≠ `absent` ≠ `invalid`) fiindcă cer acțiuni
diferite, iar un raport care le confundă trimite pe cineva să caute un bug într-un loc greșit.

Cache-ul e mărginit și versionat: o intrare per mediu, cu TTL scurt și `revision` vizibilă în
metrici. Nu e o optimizare de latență, ci limita de blast radius — fără el, fiecare accept ar
lovi control plane-ul, iar un Postgres lent ar deveni un canary oprit.

**Cache-ul NU se aplică la stări proaste.** Un `store_down` nu se memorează: altfel un incident de
30 de secunde ar ține controllerul fail-closed pentru tot TTL-ul, iar revenirea ar părea lentă
fără motiv. Se memorează doar un policy citit și validat.

## Scrierea

`apply(...)` cere `expected_revision`. Compare-and-set e în schemă (unique pe
`(environment, revision)`), deci doi operatori simultani nu pot ambii să creadă că au aplicat.
Fiecare apply scrie ȘI o urmă în `audit_log`: actor, motiv, revizia veche, revizia nouă, amprenta
policy-ului. Fără actor și motiv nu se aplică nimic — un release fără om nu e o decizie.

**Nimic din request nu ajunge aici.** `apply` e chemat exclusiv din `scripts/release_control.py`,
niciodată dintr-o rută HTTP: nu există endpoint care să accepte un procent, un `business_id` sau
un mod. Widgetul și modelul nu au cum să influențeze release trackul (P7).
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from src.db.queries.release import (
    PolicyRow,
    PolicyStoreUnavailable,
    current_policy,
    insert_policy_revision,
    write_release_audit,
)
from src.release.models import PolicyError, ReleasePolicy

log = logging.getLogger(__name__)

# ── Coduri FIXE (vocabular închis: sigure ca etichete de metrică) ───────────────────────────
POLICY_OK = "ok"
POLICY_ABSENT = "absent"
POLICY_STORE_DOWN = "store_down"
POLICY_INVALID = "invalid"
POLICY_ENV_MISMATCH = "env_mismatch"
POLICY_CODES: frozenset[str] = frozenset(
    {POLICY_OK, POLICY_ABSENT, POLICY_STORE_DOWN, POLICY_INVALID, POLICY_ENV_MISMATCH}
)


@dataclass(frozen=True, slots=True)
class PolicyView:
    """Ce știe procesul despre policy ACUM, plus cât de proaspăt e ce știe.

    `available` e despre STORE, `policy` e despre CONȚINUT. Sunt separate fiindcă „storeul merge,
    dar n-a fost aplicat niciun policy" și „storeul e jos" duc amândouă la control, dar una e
    normalitate și cealaltă e incident.
    """

    policy: ReleasePolicy | None
    revision: int | None
    code: str
    available: bool
    age_s: float = 0.0
    actor: str = ""
    applied_at: datetime | None = None

    @property
    def usable(self) -> bool:
        return self.available and self.policy is not None

    def as_props(self) -> dict[str, Any]:
        """Etichete SAFE (P12): coduri și numere, niciun ID de tenant, niciun allowlist."""
        return {
            "outcome": self.code,
            "revision": self.revision,
            "age_bucket": age_bucket(self.age_s),
        }


def age_bucket(age_s: float) -> str:
    """Vechimea cache-ului, ca vocabular închis (metricile nu primesc float-uri continue)."""
    if age_s < 5:
        return "fresh"
    if age_s < 30:
        return "recent"
    if age_s < 300:
        return "stale"
    return "very_stale"


@dataclass
class _CacheEntry:
    view: PolicyView
    fetched_monotonic: float


#: O intrare per mediu. Un proces servește un singur mediu, deci dict-ul are 1 element în
#: practică; e dict ca testele să poată izola medii fără să se calce.
_CACHE: dict[str, _CacheEntry] = {}


def reset_cache() -> None:
    """Golește cache-ul. Folosit de teste și de `release_control.py` după un apply reușit —
    altfel operatorul ar aplica un policy și ar vedea încă vechiul în `show`."""
    _CACHE.clear()


def _validate(row: PolicyRow, environment: str) -> PolicyView:
    """Rând de DB → view validat. Orice necunoscută devine cod fix, nu excepție."""
    try:
        policy = ReleasePolicy.from_payload(row.policy)
    except PolicyError as e:
        # Un document care nu mai respectă contractul (schemă schimbată, editare manuală în DB)
        # e mai periculos decât unul absent: ar putea „aproape" să funcționeze.
        log.error("NX-249: policy rev=%s invalid: %s", row.revision, e)
        return PolicyView(None, row.revision, POLICY_INVALID, available=True)
    if policy.environment != environment:
        # Un policy de staging citit de producție ar promova trafic real pe baza unei aprobări
        # date pentru altceva.
        log.error(
            "NX-249: policy rev=%s e pentru mediul %r, procesul rulează pe %r",
            row.revision,
            policy.environment,
            environment,
        )
        return PolicyView(None, row.revision, POLICY_ENV_MISMATCH, available=True)
    if policy.revision != row.revision:
        # Revizia din document trebuie să fie cea din coloană; altfel evidence packetul ar cita
        # o revizie care nu e cea în vigoare.
        log.error(
            "NX-249: policy rev=%s ≠ revision din document (%s)", row.revision, policy.revision
        )
        return PolicyView(None, row.revision, POLICY_INVALID, available=True)
    return PolicyView(
        policy=policy,
        revision=row.revision,
        code=POLICY_OK,
        available=True,
        actor=row.actor,
        applied_at=row.applied_at,
    )


async def current(
    conn: Any,
    environment: str,
    *,
    ttl_s: float = 15.0,
    force_refresh: bool = False,
) -> PolicyView:
    """Policy-ul în vigoare. NICIODATĂ ridică — pe calea de accept, o excepție ar fi tăcere (P6).

    `conn` e o conexiune de CONTROL PLANE (`admin_conn`). Vine ca argument, nu se deschide aici,
    fiindcă acceptul e deja într-un checkout scurt (NX-231) și n-are voie să deschidă altul.
    """
    now = time.monotonic()
    cached = _CACHE.get(environment)
    if cached is not None and not force_refresh and (now - cached.fetched_monotonic) < ttl_s:
        age = now - cached.fetched_monotonic
        view = cached.view
        return PolicyView(
            policy=view.policy,
            revision=view.revision,
            code=view.code,
            available=view.available,
            age_s=age,
            actor=view.actor,
            applied_at=view.applied_at,
        )
    try:
        row = await current_policy(conn, environment)
    except PolicyStoreUnavailable as e:
        log.warning("NX-249: store de policy indisponibil (%s) — fail-closed la control", e)
        return PolicyView(None, None, POLICY_STORE_DOWN, available=False)
    except Exception:  # noqa: BLE001 — orice eșec de DB e „nu știm", deci control
        log.exception("NX-249: citirea policy-ului a eșuat — fail-closed la control")
        return PolicyView(None, None, POLICY_STORE_DOWN, available=False)

    view = (
        PolicyView(None, None, POLICY_ABSENT, available=True)
        if row is None
        else _validate(row, environment)
    )
    # Se memorează DOAR stări citite cu succes (inclusiv `absent`, care e o stare stabilă).
    # `store_down` nu intră în cache: vezi docstring-ul modulului.
    _CACHE[environment] = _CacheEntry(view=view, fetched_monotonic=now)
    return view


# ── Scrierea (CAS + audit) ─────────────────────────────────────────────────────────────────
@dataclass(frozen=True, slots=True)
class ApplyResult:
    ok: bool
    revision: int | None
    reason: str

    @property
    def conflict(self) -> bool:
        return not self.ok and self.reason == "revision_conflict"


async def apply(
    conn: Any,
    policy: ReleasePolicy,
    *,
    expected_revision: int | None,
    actor: str,
    reason: str,
    environment: str,
) -> ApplyResult:
    """Aplică o revizie NOUĂ, cu compare-and-set pe revizia așteptată.

    `expected_revision=None` înseamnă „mă aștept să nu existe niciun policy" (primul apply).
    Orice nepotrivire e un refuz, nu o suprascriere: dacă altcineva a aplicat între timp, decizia
    lui poate fi un kill-switch, iar a-l suprascrie orbește ar reporni canaryul în mijlocul unui
    incident.

    Verificările sunt în ordinea costului și a autorității: forma (policy deja validat de tipul
    lui), apoi coerența (mediu, revizie), apoi CAS-ul din DB.
    """
    if not actor.strip() or not reason.strip():
        return ApplyResult(False, None, "actor_and_reason_required")
    if policy.environment != environment:
        return ApplyResult(False, None, "environment_mismatch")

    try:
        existing = await current_policy(conn, environment)
    except PolicyStoreUnavailable as e:
        return ApplyResult(False, None, f"store_unavailable: {e}")
    current_rev = existing.revision if existing else None
    if current_rev != expected_revision:
        return ApplyResult(
            False,
            current_rev,
            "revision_conflict",
        )
    next_rev = 0 if current_rev is None else current_rev + 1
    if policy.revision != next_rev:
        # Documentul trebuie să-și DECLARE revizia. Dacă am completa-o noi, amprenta calculată de
        # operator la `validate` n-ar mai fi cea persistată, iar evidence packetul ar cita alt hash.
        return ApplyResult(False, current_rev, f"policy_revision_must_be_{next_rev}")

    inserted = await insert_policy_revision(
        conn,
        environment=environment,
        revision=next_rev,
        policy_id=policy.policy_id,
        policy=policy.to_payload(),
        actor=actor,
        reason=reason,
        change_ticket=policy.change_ticket or None,
    )
    if not inserted:
        # Cursă pierdută între citire și insert — exact ce trebuie să prindă CAS-ul.
        return ApplyResult(False, current_rev, "revision_conflict")

    await write_release_audit(
        conn,
        action="release_policy_apply",
        actor=actor,
        entity_id=policy.policy_id,
        details={
            "environment": environment,
            "previous_revision": current_rev,
            "new_revision": next_rev,
            "mode": policy.mode,
            "percent": policy.percent,
            "reason": reason,
            "change_ticket": policy.change_ticket,
            "policy_fingerprint": policy.fingerprint,
            "candidate_release_sha": policy.candidate_release_sha,
            "control_release_sha": policy.control_release_sha,
            # Câți tenanți, nu CARE: auditul e citit de mai mulți ochi decât policy-ul.
            "eligible_count": len(policy.eligible_business_ids),
            "internal_count": len(policy.internal_business_ids),
        },
    )
    reset_cache()
    log.warning(
        "NX-249: policy aplicat env=%s rev=%s mode=%s percent=%s actor=%s",
        environment,
        next_rev,
        policy.mode,
        policy.percent,
        actor,
    )
    return ApplyResult(True, next_rev, "applied")


def force_control_from(
    policy: ReleasePolicy, *, now: datetime | None = None, revision: int
) -> ReleasePolicy:
    """Derivă un policy `force_control` din cel curent — kill-switchul, ca DOCUMENT.

    Deliberat nu e un flag separat pe care controllerul îl consultă în plus: kill-switchul e o
    revizie de policy ca oricare alta, deci trece prin ACELAȘI CAS, ACELAȘI audit și apare în
    ACELAȘI istoric. Un mecanism paralel de oprire ar fi un al doilea adevăr despre ce rulează.

    Se păstrează tot restul (release SHA-uri, allowlist, dovezi): oprirea nu rescrie ce era în
    canary, doar oprește accepturile noi.
    """
    stamp = (now or datetime.now(UTC)).astimezone(UTC).isoformat()
    return policy.model_copy(
        update={
            "mode": "force_control",
            "revision": revision,
            "created_at": stamp,
            "approved_at": stamp,
        }
    )
