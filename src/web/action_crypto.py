"""NX-236 — sigiliul acțiunilor: key ring, seal/open determinist, expirare, redactare.

**De ce e sigilat, nu doar semnat.** Un token semnat-dar-lizibil (JWT-style, Base64 de claims) ar
publica în browser exact ce încercăm să nu publicăm: id-uri de catalog, structura comenzilor
interne, ce kind-uri există. Un atacator n-ar putea să-l falsifice, dar ar putea să-l CITEASCĂ și
să învețe suprafața. Sigiliul AEAD rezolvă ambele: confidențialitate + integritate, într-o singură
primitivă auditată.

**De ce AES-SIV și nu AES-GCM.** GCM cere un nonce unic per mesaj; un nonce aleator ar face
tokenul NEdeterminist, iar `GET /web/v2/turns/{id}` (care re-proiectează același rând terminal) ar
întoarce alți bytes la fiecare citire — adică replay-ul n-ar mai fi byte-identic, iar orice cache
sau comparație de răspuns ar deveni zgomot. AES-SIV (RFC 5297) e construit exact pentru
determinism: aceleași claims + aceeași cheie ⇒ același ciphertext, fără nonce de gestionat și fără
catastrofa de reutilizare a lui. Primitiva vine din `cryptography` (pyca) — zero criptografie
scrisă de noi (regula cardului), doar compunere.

**Cheile.** `WEB_ACTION_KEYS = "kid:base64master[,kid_vechi:base64master]"`. Prima e CURENTĂ
(emite), toate verifică — asta e fereastra de rotație: cheia nouă intră în față, cea veche rămâne
până expiră ultimul token emis cu ea. Din fiecare master derivăm trei subchei cu HKDF (context
separat per scop, ca o scurgere într-un rol să nu compromită celelalte două):

    seal  → AES-256-SIV (sigiliul propriu-zis)
    id    → HMAC-SHA256 pentru `action_id` (id-ul e derivat, deci re-derivabil ⇒ dovadă de emitere)
    ref   → HMAC-SHA256 pentru pseudonimele de tenant/sesiune/conversație

**Ce NU face modulul:** nu citește config (primește `KeyRing`), nu cunoaște DB, nu decide
autorizarea (aia e în `action_service`) și nu loghează niciodată tokenul sau claims-urile — nici
în excepții. Un mesaj de eroare care spune „aud invalid: web-widget-v1" e un oracol.
"""

from __future__ import annotations

import base64
import binascii
import hmac
import json
import re
from dataclasses import dataclass
from typing import Any

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESSIV
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from src.web.action_models import (
    ACTION_AUDIENCE,
    ACTION_ENVELOPE_VERSION,
    MAX_ENVELOPE_BYTES,
    ActionEnvelope,
    canonical_json,
)

# Prefixul de token: versiune + key_id în CLAR (ca la orice format sigilat — trebuie să știi ce
# cheie să încerci ÎNAINTE să deschizi). Nimic comercial nu e lizibil: restul e ciphertext.
_KEY_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,16}$")
_TOKEN_RE = re.compile(r"^[A-Za-z0-9_.-]{16,4096}$")
MIN_MASTER_BYTES = 32
MAX_TOKEN_CHARS = 4096  # aliniat cu `contracts_v2.MAX_TOKEN_LEN`

# Motivele de respingere — vocabular ÎNCHIS (P10: intră în `web_action_verified{reason}`).
# Deliberat GROSIER: „bad_seal" acoperă și tamper, și cheie greșită, și claims stricate. Un motiv
# mai fin ar fi util nouă de două ori pe an și atacatorului la fiecare încercare.
CryptoReason = str
REASONS: frozenset[str] = frozenset(
    {"malformed", "unknown_key", "bad_seal", "claims", "audience", "expired", "not_yet_valid"}
)


class KeyRingError(ValueError):
    """Configurație de chei imposibilă. Ridicată la BOOT (poarta din `config`), nu sub incident."""


@dataclass(frozen=True)
class ActionKey:
    """Subcheile derivate ale unui `key_id`. Materialul master nu se păstrează: după derivare
    n-avem ce face cu el, iar un obiect care nu-l are nu-l poate scurge."""

    key_id: str
    seal: bytes  # 64B → AES-256-SIV
    id_key: bytes  # 32B → HMAC pentru action_id
    ref_key: bytes  # 32B → HMAC pentru pseudonime

    def cipher(self) -> AESSIV:
        return AESSIV(self.seal)


def _derive(master: bytes, info: bytes, length: int) -> bytes:
    return HKDF(algorithm=hashes.SHA256(), length=length, salt=None, info=info).derive(master)


def _key_from_master(key_id: str, master: bytes) -> ActionKey:
    return ActionKey(
        key_id=key_id,
        seal=_derive(master, b"nx236/action/seal/v1", 64),
        id_key=_derive(master, b"nx236/action/id/v1", 32),
        ref_key=_derive(master, b"nx236/action/ref/v1", 32),
    )


@dataclass(frozen=True)
class KeyRing:
    """Inelul de chei: prima EMITE, toate VERIFICĂ.

    Rotația e o operație de config, nu de cod: pui cheia nouă în față, deployezi, iar tokenurile
    vechi rămân valabile până le expiră TTL-ul. Ștergerea cheii vechi ÎNAINTE de expirare
    invalidează butoanele deja afișate — de asta runbook-ul cere overlap ≥ TTL."""

    keys: tuple[ActionKey, ...]

    @property
    def current(self) -> ActionKey:
        return self.keys[0]

    def get(self, key_id: str) -> ActionKey | None:
        for key in self.keys:
            # Comparație în timp constant: `key_id` vine din token, deci e input de atacator.
            if hmac.compare_digest(key.key_id, key_id):
                return key
        return None

    def slot(self, key_id: str) -> str:
        """`current` / `previous` / `unknown` — etichetă low-cardinality pentru metrici."""
        if not self.keys:
            return "unknown"
        if hmac.compare_digest(self.keys[0].key_id, key_id):
            return "current"
        return "previous" if self.get(key_id) is not None else "unknown"


def parse_key_ring(spec: str | None) -> KeyRing:
    """`"kid:base64[,kid2:base64]"` → `KeyRing`. Ridică `KeyRingError` la orice ambiguitate.

    Fail-closed prin construcție: o cheie prea scurtă, un id duplicat sau un base64 stricat
    opresc procesul la boot. Alternativa (a ignora intrarea stricată) ar însemna să pornim cu un
    inel mai mic decât credem — adică tokenuri care „expiră" inexplicabil după un deploy."""
    entries = [part.strip() for part in (spec or "").split(",") if part.strip()]
    if not entries:
        raise KeyRingError("WEB_ACTION_KEYS gol: fără chei nu se pot emite acțiuni semnate")
    keys: list[ActionKey] = []
    seen: set[str] = set()
    for entry in entries:
        key_id, _, material = entry.partition(":")
        key_id = key_id.strip()
        if not _KEY_ID_RE.match(key_id):
            raise KeyRingError("WEB_ACTION_KEYS: key_id invalid (permis: [A-Za-z0-9_-]{1,16})")
        if key_id in seen:
            raise KeyRingError(f"WEB_ACTION_KEYS: key_id duplicat ({key_id!r})")
        seen.add(key_id)
        try:
            master = base64.b64decode(material.strip(), validate=True)
        except (binascii.Error, ValueError) as e:
            raise KeyRingError(f"WEB_ACTION_KEYS: material base64 invalid pentru {key_id!r}") from e
        if len(master) < MIN_MASTER_BYTES:
            raise KeyRingError(
                f"WEB_ACTION_KEYS: cheia {key_id!r} are {len(master)}B, "
                f"minimul e {MIN_MASTER_BYTES}B"
            )
        keys.append(_key_from_master(key_id, master))
    return KeyRing(tuple(keys))


# ── Derivări cu cheie (id-ul acțiunii + pseudonimele) ───────────────────────────────────────
def pseudonym(key: ActionKey, scope: str, value: str) -> str:
    """Pseudonim STABIL per cheie pentru un id intern (tenant/sesiune/conversație).

    Se compară, nu se caută: la verificare recalculăm pseudonimul din valorile CURENTE ale
    requestului (business din sesiunea verificată, sesiunea de browser, conversația turului-sursă)
    și îl punem lângă cel din token. Un token mutat pe alt tenant nu se potrivește, iar un DB scurs
    nu dă înapoi id-urile — `scope` separă domeniile ca aceeași valoare să nu producă același
    pseudonim în două roluri."""
    mac = hmac.new(key.ref_key, f"{scope}:{value}".encode(), "sha256")
    return mac.hexdigest()[:32]


def derive_action_id(
    key: ActionKey, *, source_turn_id: str, kind: str, args: dict[str, Any]
) -> str:
    """`action_id` = HMAC peste (turul-sursă, kind, argumente canonice).

    DERIVAT, nu aleator, și asta e tot mecanismul dovezii de emitere: din planul persistat în
    tranzacția terminală (`response_json["actions"]`) se re-derivă exact aceleași id-uri. Un token
    al cărui `action_id` nu apare în setul re-derivat nu a fost emis de noi, oricât de valid ar fi
    sigiliul (failure matrix: „source turn/result absent sau action neemisă → reject")."""
    payload = canonical_json({"src": source_turn_id, "k": kind, "a": args})
    return hmac.new(key.id_key, payload.encode("utf-8"), "sha256").hexdigest()[:32]


# ── Seal / open ─────────────────────────────────────────────────────────────────────────────
def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _unb64url(text: str) -> bytes | None:
    padding = "=" * (-len(text) % 4)
    try:
        return base64.urlsafe_b64decode(text + padding)
    except (binascii.Error, ValueError):
        return None


def _aad(key_id: str) -> list[bytes]:
    """Datele asociate: versiune + key_id + audiență. Sunt în CLAR în token, deci trebuie legate
    criptografic — altfel cineva ar putea rescrie prefixul unui token valid."""
    return [ACTION_ENVELOPE_VERSION.encode(), key_id.encode(), ACTION_AUDIENCE.encode()]


def seal(key: ActionKey, envelope: ActionEnvelope) -> str:
    """`ActionEnvelope` → token opac `a1.<key_id>.<ciphertext>`. DETERMINIST."""
    claims = canonical_json(envelope.to_claims()).encode("utf-8")
    if len(claims) > MAX_ENVELOPE_BYTES:
        raise KeyRingError("envelope peste bugetul de claims (bug de construcție, nu de input)")
    sealed = key.cipher().encrypt(claims, _aad(key.key_id))
    token = f"{ACTION_ENVELOPE_VERSION}.{key.key_id}.{_b64url(sealed)}"
    if len(token) > MAX_TOKEN_CHARS:
        raise KeyRingError("token peste capul de contract (bug de construcție)")
    return token


@dataclass(frozen=True)
class OpenedToken:
    """Un token deschis: envelope-ul + ce cheie l-a deschis (metrici de rotație)."""

    envelope: ActionEnvelope
    key: ActionKey
    slot: str  # current | previous


@dataclass(frozen=True)
class OpenFailure:
    """Respingere GENERICĂ. `reason` e pentru NOI (metrici); răspunsul către client rămâne
    același indiferent de motiv — un client nu are voie să afle DE CE tokenul lui e invalid."""

    reason: CryptoReason


def open_token(
    token: str, ring: KeyRing, *, now: int, skew_s: int = 0
) -> OpenedToken | OpenFailure:
    """Token → envelope validat, sau respingere typed.

    Ordinea verificărilor e deliberată: forma (ieftin, oprește gunoiul înainte de orice
    criptografie) → cheia → sigiliul → claims → audiență → timp. Un token expirat trece prin
    sigiliu ca să știm că E al nostru — altfel „expirat" ar fi un oracol pentru orice șir."""
    if not isinstance(token, str) or len(token) > MAX_TOKEN_CHARS or not _TOKEN_RE.match(token):
        return OpenFailure("malformed")
    parts = token.split(".")
    if len(parts) != 3:
        return OpenFailure("malformed")
    version, key_id, body = parts
    if version != ACTION_ENVELOPE_VERSION or not _KEY_ID_RE.match(key_id):
        return OpenFailure("malformed")
    key = ring.get(key_id)
    if key is None:
        # Cheie necunoscută (rotată prea devreme / token dintr-un alt mediu) → fail-CLOSED.
        return OpenFailure("unknown_key")
    sealed = _unb64url(body)
    if sealed is None or len(sealed) > MAX_ENVELOPE_BYTES + 64:
        return OpenFailure("malformed")
    try:
        raw = key.cipher().decrypt(sealed, _aad(key_id))
    except (InvalidTag, ValueError):
        # Tamper pe orice segment, cheie greșită, alg confusion: TOATE arată la fel de aici.
        return OpenFailure("bad_seal")
    if len(raw) > MAX_ENVELOPE_BYTES:
        return OpenFailure("claims")
    try:
        claims = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return OpenFailure("claims")
    envelope = ActionEnvelope.from_claims(claims)
    if envelope is None:
        return OpenFailure("claims")
    if not hmac.compare_digest(envelope.audience, ACTION_AUDIENCE):
        return OpenFailure("audience")
    if envelope.expires_at + skew_s < now:
        return OpenFailure("expired")
    if envelope.issued_at - skew_s > now:
        # Ceas în urmă pe verificator sau token construit „în viitor": nu-l acceptăm ca valid.
        return OpenFailure("not_yet_valid")
    return OpenedToken(envelope=envelope, key=key, slot=ring.slot(key_id))


def redact_token(token: object) -> str:
    """Ce are voie să apară în log (P12): lungimea și atât.

    Deliberat FĂRĂ hash, nici trunchiat: un hash complet e un identificator stabil al tokenului,
    deci un prefix e destul cât să coreleze două loguri, iar corelația e exact ce n-are voie să
    existe între un buton apăsat și o conversație. `web_action_verified{reason}` spune destul."""
    length = len(token) if isinstance(token, str) else 0
    return f"tok:len={length}"


__all__ = [
    "MAX_TOKEN_CHARS",
    "MIN_MASTER_BYTES",
    "REASONS",
    "ActionKey",
    "KeyRing",
    "KeyRingError",
    "OpenFailure",
    "OpenedToken",
    "derive_action_id",
    "open_token",
    "parse_key_ring",
    "pseudonym",
    "redact_token",
    "seal",
]
