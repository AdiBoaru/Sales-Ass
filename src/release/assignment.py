"""NX-249 — asignarea: cine primește candidate, determinist, server-side și stabil în timp.

Trei proprietăți, în ordinea în care se pot strica:

1. **Determinism.** Bucketul e HMAC peste `(business_id, conversation_id)` cu un salt versionat.
   Nu `random`, nu IP, nu `visitor_id`, nu nimic din browser. Două procese, două taburi și un
   restart ajung la aceeași concluzie fără să vorbească între ele — și fără să întrebe DB-ul.

2. **Stabilitate în timp (epoch).** Procentul se aplică DOAR conversațiilor NOI. O conversație
   care are deja o asignare capturată în ledger o păstrează, oricât s-ar mișca procentul. De
   aceea `resolve` primește `prior`: creșterea 5→20 nu mută pe nimeni în mijlocul dialogului,
   iar asta e o proprietate a ALGORITMULUI, nu o convenție pe care trebuie s-o respecte apelanții.

3. **Fail-closed.** Orice necunoscută — policy absent, expirat, store căzut, controller stins —
   întoarce `control`. Niciodată candidate „din inerție", niciodată o excepție care rupe turul.

## De ce `prior` și nu „ce spune policy-ul acum"

Sticky-ul nu poate trăi în cookie/localStorage (frontendul e pasiv și nu are voie să știe de
canary) și nici doar în Redis (best-effort, se pierde la FLUSHALL). Singurul loc durabil e
ledgerul: fiecare turn poartă track-ul cu care a rulat, deci asignarea unei conversații se
re-derivă din propriul ei istoric. Un Redis pierdut nu pierde asignarea.

## Ce se întâmplă la kill-switch

`force_control` oprește accepturile candidate NOI. O conversație deja candidate NU se convertește
tăcut la control: starea, referințele ordinale și acțiunile ei au fost produse de candidate.
Conversia e permisă doar dacă policy-ul declară `rollback_compatible=True` (adică s-a DOVEDIT că
imaginea precedentă citește ce a scris candidate). Altfel conversația se DRENEAZĂ — turele active
termină, iar acceptul următor primește un error-view onest.
"""

from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass
from datetime import datetime

from src.release.models import (
    DECISION_CANDIDATE,
    DECISION_CONTROL,
    DECISION_DRAIN,
    MODE_CANARY,
    MODE_CLOSED,
    MODE_FORCE_CONTROL,
    MODE_INTERNAL,
    MODE_OBSERVE,
    REASON_BUCKET_IN,
    REASON_BUCKET_OUT,
    REASON_CLOSED,
    REASON_CONTROLLER_OFF,
    REASON_DRAIN_INCOMPATIBLE,
    REASON_FORCE_CONTROL,
    REASON_INTERNAL,
    REASON_MODE_OBSERVE,
    REASON_OUTSIDE_ADMISSION,
    REASON_POLICY_EXPIRED,
    REASON_POLICY_MISSING,
    REASON_STICKY,
    REASON_STORE_UNAVAILABLE,
    REASON_TENANT_NOT_ELIGIBLE,
    TRACK_CANDIDATE,
    TRACK_CHAMPION,
    Assignment,
    CapturedExecution,
    ReleasePolicy,
)

#: Versiunea derivării. Intră în mesajul HMAC, deci o schimbare aici REASIGNEAZĂ tot — de asta e
#: o constantă cu nume, nu un literal îngropat: schimbarea ei e o decizie, nu un refactor.
BUCKET_VERSION = "nx249.v1"

#: Câte bucketuri. 100 = un bucket per punct procentual; mai fin n-ar folosi la nimic, fiindcă
#: `percent` e întreg (și etapele sunt oricum 5/20/50/100).
BUCKETS = 100


def stable_bucket(
    business_id: str,
    conversation_id: str,
    *,
    salt: str,
    salt_id: str,
) -> int:
    """Bucket 0..99, HMAC-SHA256, determinist și neprezicibil fără salt.

    De ce HMAC și nu sha256 simplu (ca la NX-238): acolo bucketul decide un provider de
    retrieval; aici decide cine primește o versiune întreagă de produs. Cu hash public, oricine
    poate calcula bucketul oricărei conversații — și, mai important, poate ÎNCERCA `conversation_id`
    -uri până nimerește unul în canary. Cu salt secret, bucketul rămâne stabil pentru noi și
    imprevizibil pentru oricine altcineva.

    `salt_id` intră în mesaj (nu doar în cheie): o rotire de salt trebuie să fie vizibilă în
    derivare, ca reasignarea să fie explicabilă în raport, nu o distribuție care „s-a mutat".
    """
    msg = f"{BUCKET_VERSION}:{salt_id}:{business_id}:{conversation_id}".encode()
    digest = hmac.new(salt.encode("utf-8"), msg, hashlib.sha256).digest()
    return int.from_bytes(digest[:4], "big") % BUCKETS


def _control(
    reason: str, policy: ReleasePolicy | None = None, bucket: int | None = None
) -> Assignment:
    return Assignment(
        decision=DECISION_CONTROL,
        reason=reason,
        track=TRACK_CHAMPION,
        bucket=bucket,
        policy_id=policy.policy_id if policy else "",
        policy_revision=policy.revision if policy else None,
    )


def _candidate(reason: str, policy: ReleasePolicy, bucket: int | None = None) -> Assignment:
    return Assignment(
        decision=DECISION_CANDIDATE,
        reason=reason,
        track=TRACK_CANDIDATE,
        bucket=bucket,
        policy_id=policy.policy_id,
        policy_revision=policy.revision,
    )


def resolve(
    policy: ReleasePolicy | None,
    *,
    business_id: str,
    conversation_id: str,
    prior: CapturedExecution | None,
    now: datetime,
    salt: str,
    controller_enabled: bool = True,
    store_available: bool = True,
) -> Assignment:
    """Decizia pentru o conversație. PURĂ: fără DB, fără ceas intern, fără config global.

    Ordinea verificărilor e ordinea autorității:
      1. kill-switch de proces (`controller_enabled`) — dacă e stins, controllerul nu există;
      2. disponibilitatea policy-ului — absent/căzut/expirat ⇒ control, cu motiv distinct
         (un raport trebuie să poată deosebi „n-am policy" de „policy zice control");
      3. `force_control` — bate sticky-ul, dar nu convertește forțat o conversație candidate;
      4. sticky — o conversație cu istoric își păstrează track-ul;
      5. abia apoi allowlist, fereastră de admisie și bucket.

    `prior` e captura de pe cel mai recent turn al conversației (`None` = conversație nouă SAU
    ture de dinainte de migrarea 044 — ambele se tratează ca „nouă", ceea ce e sigur: la o
    conversație veche fără captură, decizia proaspătă e control dacă bucketul o cere).
    """
    if not controller_enabled:
        return _control(REASON_CONTROLLER_OFF)
    if not store_available:
        # Storeul de config indisponibil: fail-closed EXPLICIT, cu motiv propriu. Nu-l amestecăm
        # cu „policy lipsă" — primul e un incident de infrastructură, al doilea o stare normală.
        return _control(REASON_STORE_UNAVAILABLE)
    if policy is None:
        return _control(REASON_POLICY_MISSING)
    if not policy.is_valid_at(now):
        # Un policy expirat nu se prelungește singur. Expirarea E mecanismul prin care un canary
        # uitat se oprește de la sine, în loc să curgă la nesfârșit.
        return _control(REASON_POLICY_EXPIRED, policy)

    prior_candidate = prior is not None and prior.track == TRACK_CANDIDATE

    if policy.mode == MODE_FORCE_CONTROL:
        if prior_candidate and not policy.rollback_compatible:
            # Conversația a fost servită de candidate: starea, referințele și acțiunile ei vin de
            # acolo. Fără compatibilitate DOVEDITĂ, mutarea pe control i-ar schimba înțelesul
            # („al doilea" ar arăta alt produs). Drenăm și spunem adevărul clientului.
            return Assignment(
                decision=DECISION_DRAIN,
                reason=REASON_DRAIN_INCOMPATIBLE,
                track=None,
                policy_id=policy.policy_id,
                policy_revision=policy.revision,
            )
        return _control(REASON_FORCE_CONTROL, policy)

    if prior is not None:
        # EPOCH: track-ul capturat e autoritatea. Procentul nu re-decide nimic aici — de asta
        # creșterea 5→20 e sigură prin construcție, nu prin grija apelantului.
        if prior_candidate:
            return _candidate(REASON_STICKY, policy)
        return _control(REASON_STICKY, policy)

    # ── conversație NOUĂ ───────────────────────────────────────────────────────────────────
    if policy.mode == MODE_OBSERVE:
        return _control(REASON_MODE_OBSERVE, policy)

    if policy.mode == MODE_CLOSED:
        # Etapa 7: v1 e închis public, candidate e default. Allowlistul nu mai filtrează nimic —
        # dacă ar filtra, tenanții din afara lui ar rămâne pe o rută care tocmai s-a închis.
        return _candidate(REASON_CLOSED, policy)

    if policy.is_internal(business_id):
        # Cohortul intern trece peste procent în ORICE mod care livrează: „intern" e o listă de
        # tenanți, nu o proporție. La `internal` e singura poartă; la `canary` e dogfooding.
        return _candidate(REASON_INTERNAL, policy)

    if policy.mode == MODE_INTERNAL:
        return _control(REASON_TENANT_NOT_ELIGIBLE, policy)

    assert policy.mode == MODE_CANARY
    if not policy.admits_new_at(now):
        # Fereastra de admisie s-a închis (drain planificat): conversațiile existente continuă,
        # cele noi nu mai intră în epoch.
        return _control(REASON_OUTSIDE_ADMISSION, policy)
    if not policy.is_eligible(business_id):
        return _control(REASON_TENANT_NOT_ELIGIBLE, policy)

    bucket = stable_bucket(business_id, conversation_id, salt=salt, salt_id=policy.stable_salt_id)
    if bucket < policy.percent:
        return _candidate(REASON_BUCKET_IN, policy, bucket=bucket)
    return _control(REASON_BUCKET_OUT, policy, bucket=bucket)


@dataclass(frozen=True, slots=True)
class ReleaseContext:
    """Tot ce trebuie ca să decizi, împachetat o dată per request.

    Există ca să NU se ducă cinci parametri prin `accept_web_turn` (policy, disponibilitate, salt,
    flag, ceas) — dar și pentru o proprietate mai importantă: contextul se construiește la MARGINE,
    unde se poate deschide o conexiune de control plane, și se CONSUMĂ în checkout-ul de accept,
    unde nu se mai are voie (NX-231). Un `None` pe drumul de accept înseamnă „controller stins" și
    produce byte-identic comportamentul de dinainte de card.

    Ceasul e capturat aici, o singură dată: două citiri de `now()` în timpul unei decizii pot cădea
    de o parte și de alta a expirării unui policy, iar asignarea ar deveni nedeterministă exact la
    granița în care contează.
    """

    policy: ReleasePolicy | None
    available: bool
    salt: str
    now: datetime
    enabled: bool = True

    def decide(
        self, business_id: str, conversation_id: str, prior: CapturedExecution | None
    ) -> Assignment:
        return resolve(
            self.policy,
            business_id=business_id,
            conversation_id=conversation_id,
            prior=prior,
            now=self.now,
            salt=self.salt,
            controller_enabled=self.enabled,
            store_available=self.available,
        )

    @property
    def mode(self) -> str:
        """Modul curent, pentru etichete de metrică. Fără policy: `observe` (nu livrăm nimic)."""
        return self.policy.mode if self.policy else MODE_OBSERVE


def distribution(
    policy: ReleasePolicy,
    pairs: list[tuple[str, str]],
    *,
    salt: str,
) -> dict[int, int]:
    """Histograma bucketurilor pentru o listă de `(business_id, conversation_id)`.

    Folosită de `scripts/release_control.py plan` ca să dovedească uniformitatea ÎNAINTE de a
    aplica un procent: un bug de hash se vede ca o distribuție strâmbă, nu ca un incident peste
    două zile. Nu întoarce ID-uri, doar numărători — raportul nu are voie să conțină identificatori.
    """
    hist: dict[int, int] = {}
    for business_id, conversation_id in pairs:
        b = stable_bucket(business_id, conversation_id, salt=salt, salt_id=policy.stable_salt_id)
        hist[b] = hist.get(b, 0) + 1
    return hist


def chi_square_uniformity(hist: dict[int, int], *, buckets: int = BUCKETS) -> float:
    """χ² față de uniform. Pur diagnostic: `plan` îl publică, nu îl transformă în verdict.

    Motivul e onestitatea statistică: cu 10.000 de ID-uri sintetice și 100 de bucketuri, pragul
    critic la 5% e ~123 (99 g.l.). O valoare mult peste înseamnă „uită-te la hash", nu „blochează
    releaseul" — decizia rămâne umană, ca peste tot în cardul ăsta.
    """
    total = sum(hist.values())
    if total == 0:
        return 0.0
    expected = total / buckets
    return sum((hist.get(b, 0) - expected) ** 2 / expected for b in range(buckets))
