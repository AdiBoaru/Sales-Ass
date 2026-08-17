"""NX-248 — manifestul de deploy: singurul loc care spune ce artefact rulează și pe ce se sprijină.

Fără el, „ce e în producție?" se răspunde prin trei surse care se contrazic: un tag mutabil, un
`git log` de pe host și memoria cuiva. Manifestul le înlocuiește cu un obiect: release SHA, digest
de imagine, revizie de config, interval de schemă tolerat, digestul PRECEDENT și amprentele
dovezilor (SBOM, scan, semnătură).

## Trei proprietăți care fac diferența dintre un manifest și un fișier de notițe

  1. **Amprentă canonică.** JSON sortat, separatori fixați, fără spații — două serializări ale
     aceleiași stări dau aceiași bytes, deci `fingerprint` chiar detectează o editare. (Aceeași
     disciplină ca projectorul pur NX-240 și artefactul de decizie NX-238.)
  2. **`previous_digest` e parte din manifest, nu din amintiri.** Rollbackul are nevoie de o
     țintă; dacă ținta se deduce în incident, se deduce greșit.
  3. **Fezabilitatea rollbackului e o întrebare cu răspuns.** `rollback_possible()` compară
     intervalul de schemă al imaginii precedente cu schema APLICATĂ. Dacă schema a depășit ce
     tolerează imaginea veche, releaseul trebuie să se blocheze ÎNAINTE de deploy — cardul o cere
     explicit, iar motivul e că singurul moment în care descoperi altfel e incidentul.

Semnătura HMAC e opțională și separată de semnătura imaginii: cosign atestă BYTES-ii imaginii,
manifestul atestă DECIZIA de release (ce digest, peste ce schemă, cu ce config). Sunt două
afirmații diferite; una nu o poate înlocui pe cealaltă.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from dataclasses import asdict, dataclass, field
from typing import Any

MANIFEST_VERSION = "deploy-manifest.v1"


@dataclass(frozen=True, slots=True)
class DeployManifest:
    """Ce se promovează, peste ce, și cu ce dovezi."""

    version: str
    image: str
    digest: str
    release_sha: str
    built_at: str
    config_revision: str
    schema_requires: int
    schema_tolerates: int
    previous_digest: str = ""
    #: Intervalul de schemă al imaginii PRECEDENTE — necesar ca să știm dacă rollbackul e posibil.
    #: `-1` = necunoscut (primul release): tratat ca „nu pot promite rollback", nu ca „e în regulă".
    previous_schema_tolerates: int = -1
    run_id: str = ""
    #: Amprente ale dovezilor, nu dovezile: un manifest nu poartă un SBOM de câțiva MB.
    evidence: dict[str, str] = field(default_factory=dict)

    def canonical(self) -> str:
        """Serializare canonică (sortată, compactă). Baza amprentei ȘI a semnăturii."""
        payload = {k: v for k, v in asdict(self).items() if k not in ("fingerprint", "signature")}
        return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)

    def fingerprint(self) -> str:
        return hashlib.sha256(self.canonical().encode("utf-8")).hexdigest()

    def sign(self, key: str | None) -> str:
        """HMAC peste forma canonică. Gol fără cheie — și asta e vizibil în artefact, nu ascuns."""
        if not key:
            return ""
        return hmac.new(key.encode("utf-8"), self.canonical().encode("utf-8"), "sha256").hexdigest()

    def rollback_possible(self, applied_schema: int) -> tuple[bool, str]:
        """Se poate reveni la `previous_digest` peste schema APLICATĂ?

        Trei „nu"-uri distincte, fiindcă cer trei acțiuni diferite:
          • fără digest precedent (primul release) — nu există țintă;
          • interval necunoscut al imaginii precedente — nu promitem ce nu putem verifica;
          • schema a depășit ce tolerează imaginea veche — contractul expand/contract a fost rupt,
            deci trebuie un release intermediar, nu un rollback.
        """
        if not self.previous_digest:
            return False, "fără digest precedent (primul release)"
        if self.previous_schema_tolerates < 0:
            return False, "intervalul de schemă al imaginii precedente e necunoscut"
        if applied_schema > self.previous_schema_tolerates:
            return False, (
                f"schema aplicată ({applied_schema:03d}) depășește ce tolerează imaginea "
                f"precedentă ({self.previous_schema_tolerates:03d}) — rollbackul ar rula cod orb "
                "peste schema nouă"
            )
        return True, "ok"

    def to_json(self, key: str | None = None) -> str:
        """Artefactul de pe disc: starea + amprenta + (opțional) semnătura."""
        payload: dict[str, Any] = json.loads(self.canonical())
        payload["fingerprint"] = self.fingerprint()
        payload["signature"] = self.sign(key)
        return json.dumps(payload, sort_keys=True, indent=2, ensure_ascii=False) + "\n"


class ManifestError(ValueError):
    """Manifest absent, editat, nesemnat sau cu amprentă care nu corespunde conținutului."""


def load(raw: str, *, key: str | None = None, require_signature: bool = False) -> DeployManifest:
    """Citește și VERIFICĂ un manifest. Fail-closed pe fiecare ramură.

    Ordinea verificărilor nu e întâmplătoare: întâi forma (câmpuri obligatorii), apoi amprenta
    (conținut neschimbat), apoi semnătura (conținut AUTORIZAT). O semnătură validată înaintea
    amprentei ar fi validat un obiect pe care nu l-am reconstruit încă.
    """
    try:
        data = json.loads(raw)
    except ValueError as e:
        raise ManifestError(f"manifest ilizibil: {type(e).__name__}") from e
    if not isinstance(data, dict):
        raise ManifestError("manifestul nu e un obiect JSON")

    claimed_fp = data.pop("fingerprint", "")
    claimed_sig = data.pop("signature", "")
    try:
        manifest = DeployManifest(**data)
    except TypeError as e:
        raise ManifestError(f"manifest cu câmpuri lipsă/necunoscute: {e}") from e
    if manifest.version != MANIFEST_VERSION:
        raise ManifestError(f"versiune de manifest necunoscută: {manifest.version!r}")
    if claimed_fp != manifest.fingerprint():
        raise ManifestError("amprenta nu corespunde conținutului — manifestul a fost editat")
    if require_signature:
        if not key:
            raise ManifestError("se cere semnătură, dar RELEASE_MANIFEST_KEY lipsește")
        if not claimed_sig or not hmac.compare_digest(claimed_sig, manifest.sign(key)):
            raise ManifestError("semnătura manifestului nu se verifică")
    return manifest
