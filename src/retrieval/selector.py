"""NX-238 — selectorul de provider: singura poartă prin care candidatul poate ajunge live.

Trei lucruri, în ordinea în care contează:

1. **Fără GO semnat, candidatul nu poate fi selectat.** Nu „nu ar trebui" — nu poate:
   `select_provider`
   întoarce `current_live` pentru orice verdict care nu e un GO verificat, iar `build_port` refuză
   să construiască adaptorul candidat fără o selecție care îl numește. Un flag pornit din greșeală
   într-un `.env` nu e suficient; trebuie și artefactul de decizie, cu amprentă validă și semnătură.
2. **Verdictul e un ARTEFACT, nu o opinie.** `decision.json` poartă un `fingerprint` recalculabil
   propriul conținut și o `signature` HMAC peste el. Cine editează fișierul ca să scrie „GO" rupe
   amprenta; cine recalculează amprenta n-are cheia. Ambele eșecuri au cod fix și duc în același
   loc: traseul live curent.
3. **Selecția e stabilă și server-side.** Bucketul se derivă determinist din `(business_id,
   conversation_id)` — aceeași conversație primește același provider la fiecare tur, deci nu se
   comută providerul în mijlocul unei discuții. Nici modelul, nici frontendul nu ating alegerea.

Verdictul curent, măsurat: **NOT-READY** (vezi `reports/nx238/`). Candidatul rămâne inert.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from src.config import get_settings
from src.retrieval.current_live import PROVIDER_VERSION as CURRENT_LIVE_VERSION
from src.retrieval.search_entities import PROVIDER_VERSION as SEARCH_ENTITIES_VERSION

log = logging.getLogger(__name__)

#: Verdictele legitime. Cardul e completabil pe oricare — doar unul permite promovarea.
VERDICT_GO = "GO"
VERDICT_NO_GO = "NO_GO"
VERDICT_NOT_READY = "NOT_READY"
_VERDICTS = frozenset({VERDICT_GO, VERDICT_NO_GO, VERDICT_NOT_READY})

#: Coduri FIXE de blocare — vocabular închis, sigur în `retrieval_gate{decision,blocking_code}`.
BLOCK_FLAG_OFF = "candidate_flag_off"
BLOCK_ARTIFACT_MISSING = "decision_artifact_missing"
BLOCK_ARTIFACT_MALFORMED = "decision_artifact_malformed"
BLOCK_FINGERPRINT_MISMATCH = "decision_fingerprint_mismatch"
BLOCK_UNSIGNED = "decision_unsigned"
BLOCK_SIGNATURE_INVALID = "decision_signature_invalid"
BLOCK_NO_SIGNING_KEY = "decision_no_signing_key"
BLOCK_NOT_GO = "decision_not_go"
BLOCK_MANIFEST_DRIFT = "decision_manifest_drift"
BLOCK_OUT_OF_BUCKET = "candidate_out_of_rollout_bucket"

#: Câmpurile care NU intră în amprentă: amprenta însăși și semnătura peste ea.
_UNSIGNED_FIELDS = ("fingerprint", "signature")


@dataclass(frozen=True, slots=True)
class PromotionDecision:
    """Artefactul de decizie, așa cum a fost citit de pe disc (încă neverificat)."""

    verdict: str
    decided_by: str = ""
    decided_at: str = ""
    manifest: dict[str, Any] | None = None
    blocking_codes: tuple[str, ...] = ()
    fingerprint: str = ""
    signature: str = ""
    payload: dict[str, Any] | None = None

    @property
    def claims_go(self) -> bool:
        return self.verdict == VERDICT_GO


@dataclass(frozen=True, slots=True)
class ProviderSelection:
    """Ce provider rulează turul, de ce, și sub ce versiune de pipeline."""

    provider_version: str
    reason: str
    pipeline_version: str
    bucket: int | None = None
    blocking_code: str | None = None

    @property
    def is_candidate(self) -> bool:
        return self.provider_version == SEARCH_ENTITIES_VERSION


def canonical_payload(payload: dict[str, Any]) -> str:
    """Serializare DETERMINISTĂ a artefactului, fără amprentă/semnătură.

    Chei sortate și separatori fixați: aceleași date produc aceiași bytes pe orice mașină, deci o
    amprentă diferită înseamnă conținut diferit, nu formatare diferită.
    """
    body = {k: v for k, v in payload.items() if k not in _UNSIGNED_FIELDS}
    return json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def compute_fingerprint(payload: dict[str, Any]) -> str:
    """SHA-256 peste conținutul canonic. Prefixat, ca să nu fie confundat cu alt hash."""
    digest = hashlib.sha256(canonical_payload(payload).encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


def sign_fingerprint(fingerprint: str, key: str) -> str:
    """HMAC-SHA256 peste amprentă. Cheia e un secret de operare, nu o valoare din repo."""
    if not key:
        raise ValueError("semnarea unui verdict cere o cheie (RETRIEVAL_DECISION_KEY)")
    return hmac.new(key.encode("utf-8"), fingerprint.encode("utf-8"), "sha256").hexdigest()


def load_decision(path: str | Path) -> tuple[PromotionDecision | None, str | None]:
    """Citește artefactul. Întoarce `(decizie, cod_de_blocare)` — niciodată o excepție.

    Un artefact lipsă sau stricat NU e o eroare de rulare: e exact starea „nu avem GO", care are
    un răspuns clar (traseul live). A arunca aici ar transforma o poartă închisă într-un incident.
    """
    file_path = Path(path)
    try:
        raw = json.loads(file_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None, BLOCK_ARTIFACT_MISSING
    except (OSError, ValueError):
        log.warning("NX-238: artefactul de decizie nu poate fi citit/parsat")
        return None, BLOCK_ARTIFACT_MALFORMED

    if not isinstance(raw, dict):
        return None, BLOCK_ARTIFACT_MALFORMED
    verdict = raw.get("verdict")
    if verdict not in _VERDICTS:
        return None, BLOCK_ARTIFACT_MALFORMED

    manifest = raw.get("manifest")
    blocking = raw.get("blocking_codes")
    return (
        PromotionDecision(
            verdict=str(verdict),
            decided_by=str(raw.get("decided_by") or ""),
            decided_at=str(raw.get("decided_at") or ""),
            manifest=manifest if isinstance(manifest, dict) else None,
            blocking_codes=tuple(str(c) for c in blocking) if isinstance(blocking, list) else (),
            fingerprint=str(raw.get("fingerprint") or ""),
            signature=str(raw.get("signature") or ""),
            payload=raw,
        ),
        None,
    )


def verify_decision(
    decision: PromotionDecision | None,
    *,
    key: str,
    expected_manifest: dict[str, Any] | None = None,
) -> str | None:
    """`None` = GO valid și promovabil. Altfel, codul FIX care spune de ce nu.

    Ordinea verificărilor e cea a costului: forma întâi, criptografia la urmă. Toate rulează
    fail-closed — orice necunoscută înseamnă „nu promovăm".
    """
    if decision is None:
        return BLOCK_ARTIFACT_MISSING
    if not decision.claims_go:
        return BLOCK_NOT_GO
    if decision.payload is None:
        return BLOCK_ARTIFACT_MALFORMED
    if not decision.decided_by.strip():
        # Un GO fără om care semnează nu e o decizie; software-ul nu poate emite GO singur.
        return BLOCK_UNSIGNED
    if compute_fingerprint(decision.payload) != decision.fingerprint:
        return BLOCK_FINGERPRINT_MISMATCH
    if not decision.signature:
        return BLOCK_UNSIGNED
    if not key:
        # Fără cheie configurată nu putem verifica nimic — deci nu avem voie să credem nimic.
        return BLOCK_NO_SIGNING_KEY
    if not hmac.compare_digest(sign_fingerprint(decision.fingerprint, key), decision.signature):
        return BLOCK_SIGNATURE_INVALID
    if expected_manifest:
        actual = decision.manifest or {}
        if any(actual.get(k) != v for k, v in expected_manifest.items()):
            # Catalog/qrels/policy s-au mișcat sub verdict: decizia a fost luată pe altă lume.
            return BLOCK_MANIFEST_DRIFT
    return None


def stable_bucket(business_id: str, conversation_id: str) -> int:
    """Bucket determinist 0-99 din `(business_id, conversation_id)`.

    Stabil = aceeași conversație cade mereu în același bucket, deci ramp-ul nu comută providerul
    în mijlocul unei discuții. Derivat din hash, nu din random: două procese trebuie să ajungă la
    aceeași concluzie fără să vorbească între ele.
    """
    digest = hashlib.sha256(f"nx238:{business_id}:{conversation_id}".encode()).digest()
    return int.from_bytes(digest[:4], "big") % 100


def select_provider(
    *,
    business_id: str,
    conversation_id: str,
    settings: Any | None = None,
    decision: PromotionDecision | None = None,
    expected_manifest: dict[str, Any] | None = None,
) -> ProviderSelection:
    """Alege providerul turului. Fără GO verificat → `current_live`, întotdeauna."""
    cfg = settings or get_settings()
    pipeline_version = str(getattr(cfg, "retrieval_pipeline_version", "retrieval.v1"))

    def live(
        reason: str, blocking_code: str | None, bucket: int | None = None
    ) -> ProviderSelection:
        return ProviderSelection(
            provider_version=CURRENT_LIVE_VERSION,
            reason=reason,
            pipeline_version=pipeline_version,
            bucket=bucket,
            blocking_code=blocking_code,
        )

    if not getattr(cfg, "retrieval_candidate_enabled", False):
        # Kill switch-ul e prima verificare: OFF înseamnă că nici nu atingem discul.
        return live("kill_switch_off", BLOCK_FLAG_OFF)

    if decision is None:
        decision, load_block = load_decision(
            getattr(cfg, "retrieval_decision_path", "reports/nx238/decision.json")
        )
        if load_block:
            return live("no_signed_go", load_block)

    block = verify_decision(
        decision,
        key=str(getattr(cfg, "retrieval_decision_key", "") or ""),
        expected_manifest=expected_manifest,
    )
    if block:
        return live("no_signed_go", block)

    bucket = stable_bucket(business_id, conversation_id)
    rollout = max(0, min(100, int(getattr(cfg, "retrieval_candidate_rollout_pct", 0))))
    if bucket >= rollout:
        return live("out_of_rollout", BLOCK_OUT_OF_BUCKET, bucket=bucket)

    return ProviderSelection(
        provider_version=SEARCH_ENTITIES_VERSION,
        reason="signed_go_canary",
        pipeline_version=pipeline_version,
        bucket=bucket,
    )


def build_port(ctx: Any, deps: Any, selection: ProviderSelection, **kwargs: Any) -> Any:
    """Selecție → adaptorul concret. A doua încuietoare pe aceeași ușă.

    Chiar dacă cineva construiește manual o `ProviderSelection` cu providerul candidat, ea trebuie
    să fi trecut prin `select_provider` ca să existe — iar orice valoare necunoscută cade pe
    traseul live, nu pe o excepție (principiul 6: niciodată tăcere, niciodată rupere de tur).
    """
    if selection.provider_version == SEARCH_ENTITIES_VERSION:
        from src.retrieval.search_entities import SearchEntitiesAdapter  # noqa: PLC0415

        return SearchEntitiesAdapter(ctx, deps, **kwargs)
    if selection.provider_version != CURRENT_LIVE_VERSION:
        log.warning("NX-238: provider necunoscut %r → current live", selection.provider_version)
    from src.retrieval.current_live import CurrentLiveRetrievalAdapter  # noqa: PLC0415

    return CurrentLiveRetrievalAdapter(ctx, deps)
