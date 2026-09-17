"""NX-228 — Contractul `web-turn.v2` / `web-view.v2`: backendul livrează un ViewModel
**display-ready**, frontendul îl randează cu `switch(block.type)` și atât.

Pur: fără DB, fără LLM, fără I/O. Inert în acest card — nicio rută nu îl servește încă
(vine în NX-232/233). V1 (`src/channels/web/render.py` + `docs/FRONTEND-CONTRACT-IZI.md`)
rămâne NEATINS până la cutoverul NX-249.

**De ce există.** În v1 frontendul e un al doilea motor: își calculează procentul de reducere
(`ChatProductCard.jsx:266`), își ghicește tonul unui badge dintr-un regex peste cuvinte
românești (`inferBadgeTone`, `:64`), își parsează prețurile din string cu euristici de virgulă
(`chatClient.js:103`), alege comportamentul CTA-ului după `offer.kind` (`ChatOffer.jsx:29`),
compune mesaje din numele produsului (`:304`) și acumulează criterii de conversație (`:404`).
Fiecare dintre ele e o regulă comercială care trăiește în două locuri și diverge în tăcere.

**Regula pe care o impune tipul, nu disciplina.** Tot ce e afișabil e deja `str` localizat:
prețul e `"89,00 lei"`, nu `89.0`; reducerea e `"-18%"`, nu doi termeni de scăzut; stocul e
`"Ultimele 3 bucăți"`, nu `3`. Un `float` în contract e o invitație la aritmetică în browser,
deci nu există niciun `float` în ViewModel. Cifrele rămân în backend, unde există validator.

**Trei invarianți structurali (nu convenții):**
  1. `extra="forbid"` peste tot + union discriminat FINIT → un `block.type` necunoscut sau un
     câmp în plus respinge ÎNTREG payloadul. Fără skip/omit best-effort: un renderer care sare
     peste ce nu înțelege afișează un răspuns pe jumătate și îl numește succes.
  2. Terminalele `completed|failed|cancelled` au minimum un bloc randabil, impus de
     `model_validator`. P6: nicio cale terminală nu produce tăcere.
  3. Copy-ul de chrome/a11y e obligatoriu și non-blank. Un label lipsă nu e „FE-ul pune ceva
     implicit", e contract invalid — altfel microcopy-ul comercial se întoarce în browser pe
     ușa din dos.

**Untrusted by default.** `PageContextClaim` se numește *claim*, nu *context*: e ce AFIRMĂ
browserul. Niciun câmp din el nu e adevăr până la rehidratarea server-side din NX-234, iar
`business_id` nu apare în request deloc — e server-owned (P7), niciodată din browser.
"""

from __future__ import annotations

import hashlib
import json
from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

# ── Versiuni ────────────────────────────────────────────────────────────────────────────────
# Major-ul e în NUME, nu într-un câmp numeric: un client care nu cunoaște `web-view.v2` nu are
# ce „interpreta best-effort", ci refuză contractul (vezi `parse_view`).
TURN_SCHEMA_VERSION = "web-turn.v2"


# ── Limite (impuse în COD, nu în prompt — P4) ───────────────────────────────────────────────
MAX_MESSAGES = 4
MAX_BLOCKS_PER_MESSAGE = 12
MAX_PRODUCT_ITEMS = 6  # aceeași limită ca tool results (CLAUDE.md: max 6 produse)
MAX_COMPARISON_COLUMNS = 3
MAX_COMPARISON_ROWS = 12
MAX_ACTIONS_PER_ROW = 5  # `_MAX_WEB_CHIPS` din render.py v1 — widgetul nu trebuie să pară încărcat
MAX_ACTIONS_PER_ITEM = 3
MAX_KEY_VALUE_ROWS = 12
MAX_STATUS_ITEMS = 8
MAX_ROUTINE_STEPS = 8
MAX_MEMORY_CRITERIA = 8
MAX_CART_LINES = 20
MAX_BADGES = 3

MAX_TEXT_LEN = 2000
MAX_TITLE_LEN = 200
MAX_LABEL_LEN = 60
# `models.MAX_CHIP_LEN` (v1 `_MAX_WEB_CHIP_LEN`): un chip e MESAJUL pe care l-ar scrie clientul,
# nu o etichetă de două cuvinte. Valoarea e duplicată intenționat (modulul ăsta rămâne pur, fără
# import din pipeline), dar trebuie să nu scadă sub cea din v1: aici un label prea lung nu se taie,
# ci INVALIDEAZĂ tot ViewModel-ul (`extra="forbid"` + `_bounded`), deci ar fi un răspuns pierdut.
MAX_ACTION_LABEL_LEN = 56
MAX_VALUE_LEN = 200
MAX_URL_LEN = 2048
MAX_TOKEN_LEN = 4096  # token opac semnat (NX-236); FE nu îl citește, doar îl retransmite
MAX_OPAQUE_ID_LEN = 128


# ── Primitive de string ─────────────────────────────────────────────────────────────────────
# `strip_whitespace` ÎNAINTE de `min_length` → `" "` e respins ca `""`. Un label „gol deghizat"
# ar trece de un `min_length=1` naiv și ar produce un buton fără nume.
def _bounded(max_length: int) -> Any:
    return Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=1, max_length=max_length)
    ]


Text = _bounded(MAX_TEXT_LEN)
Title = _bounded(MAX_TITLE_LEN)
Label = _bounded(MAX_LABEL_LEN)
ActionLabel = _bounded(MAX_ACTION_LABEL_LEN)
Value = _bounded(MAX_VALUE_LEN)
OpaqueId = _bounded(MAX_OPAQUE_ID_LEN)
OpaqueToken = _bounded(MAX_TOKEN_LEN)


# ── Vocabulare ÎNCHISE ──────────────────────────────────────────────────────────────────────
# `tone` e semantic, nu cromatic: backendul spune CE ÎNSEAMNĂ, FE alege culoarea. FE-ul canonic
# cunoaște în plus `promo`; v2 nu îl emite — se adaugă printr-un minor cu schema hash negociat,
# nu prin „merge oricum, are fallback".
Tone = Literal["neutral", "info", "success", "warning", "danger"]

# `appearance` e rolul vizual al unui control, nu clasa lui CSS.
Appearance = Literal["primary", "secondary", "chip", "link", "danger"]

# Allowlist de iconuri = exact cele pe care rendererul canonic le are mapate
# (`ChatProductCard.jsx:73`). Backendul nu are voie să ceară un icon inexistent: ar produce un
# rând mut. Niciodată SVG sau URL — un icon e un NUME, nu conținut executabil.
Icon = Literal["truck", "tag", "percent", "shield", "clock", "gift", "info", "check", "alert"]

TextVariant = Literal["lead", "body", "caption", "disclosure"]
NoticeLevel = Literal["info", "success", "warning", "error"]

# Statusul de sârmă, exact cele șase. `turn.status`, NU `status` la top level: în v1 `status` e
# stocul unui produs, iar două înțelesuri pe același nume pe același canal e o capcană.
TurnStatus = Literal["accepted", "working", "validating", "completed", "failed", "cancelled"]
TERMINAL_STATUSES: frozenset[str] = frozenset({"completed", "failed", "cancelled"})

# Suprafața paginii gazdă — AFIRMATĂ de browser, adevăr abia după rehidratarea NX-234.
Surface = Literal["home", "category", "product", "cart", "checkout", "order", "other"]


# ── URL: allowlist de scheme ────────────────────────────────────────────────────────────────
# `https` absolut sau rută relativă („/p/ser-x"). Explicit INTERZISE: `javascript:`, `data:`,
# `file:`, `vbscript:` și `http` în clar. Verificarea e pe VALOAREA parsată, nu pe un regex de
# blocare: o listă de lucruri interzise se ocolește, o listă de lucruri permise nu.
_FORBIDDEN_URL_PREFIXES = ("javascript:", "data:", "file:", "vbscript:", "blob:", "about:")


def _validate_url(raw: str) -> str:
    value = raw.strip()
    if not value:
        raise ValueError("URL gol")
    if len(value) > MAX_URL_LEN:
        raise ValueError(f"URL peste {MAX_URL_LEN} caractere")
    lowered = value.lower()
    # `\` și whitespace intern sunt vectori clasici de bypass ("java\nscript:"); le respingem
    # înainte de orice comparație de prefix.
    if any(ch.isspace() for ch in value) or "\\" in value:
        raise ValueError("URL cu whitespace sau backslash")
    if lowered.startswith(_FORBIDDEN_URL_PREFIXES):
        raise ValueError(f"schemă de URL interzisă: {value[:24]!r}")
    if value.startswith("//"):
        # Protocol-relative: moștenește schema paginii gazdă, deci poate ajunge `http:`.
        raise ValueError("URL protocol-relativ (//) nu e permis")
    if value.startswith("/"):
        return value
    if lowered.startswith("https://"):
        return value
    raise ValueError("URL trebuie să fie https:// absolut sau rută relativă /…")


SafeUrl = Annotated[str, Field(max_length=MAX_URL_LEN)]


class _Base(BaseModel):
    """Bază comună: `extra="forbid"` peste tot. Un câmp în plus nu e ignorat, e eroare — altfel
    un backend mai nou poate „strecura" semantică pe care rendererul nu o afișează."""

    model_config = ConfigDict(extra="forbid", frozen=True)


# ── Request (browser → backend) ─────────────────────────────────────────────────────────────
class TextInput(_Base):
    type: Literal["text"]
    text: Text


class ActionInput(_Base):
    """Retrimiterea NESCHIMBATĂ a unui token opac. FE nu îl decodează, nu îl compune, nu îl
    completează — dacă ar putea, ar deveni din nou un motor de intenții (NX-236)."""

    type: Literal["action"]
    action_token: OpaqueToken


TurnInput = Annotated[TextInput | ActionInput, Field(discriminator="type")]


class PageContextClaim(_Base):
    """Ce AFIRMĂ pagina gazdă despre unde se află clientul. ID-uri opace, niciodată fapte:
    zero preț, zero nume, zero stoc, zero stare de conversație. NX-234 rehidratează server-side;
    până atunci fiecare câmp e neîncredere, inclusiv `locale`.

    `business_id` lipsește intenționat și nu se adaugă niciodată aici (P7)."""

    surface: Surface = "other"
    product_id: OpaqueId | None = None
    variant_id: OpaqueId | None = None
    category_id: OpaqueId | None = None
    cart_ref: OpaqueId | None = None
    locale: Label | None = None


class WebTurnRequestV2(_Base):
    """Un tur cerut de browser. `client_turn_id` e OBLIGATORIU: e cheia de idempotency pe care
    NX-232 întoarce exact același rezultat fără al doilea apel LLM. Fără el nu există replay,
    deci nu există recovery la refresh."""

    schema_version: Literal["web-turn.v2"]
    client_turn_id: UUID
    input: TurnInput
    context: PageContextClaim = PageContextClaim()
    id_token: OpaqueToken | None = None


# ── Schema + parse (cerere) ─────────────────────────────────────────────────────────────────


def turn_json_schema() -> dict[str, Any]:
    """JSON Schema al CERERII — consumat de generatorul de validator din repo-ul de frontend."""
    return WebTurnRequestV2.model_json_schema()


def _canonical(schema: dict[str, Any]) -> str:
    return json.dumps(schema, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def schema_hash(schema: dict[str, Any] | None = None) -> str:
    """Amprenta unei scheme — moneda negocierii de capabilitate între backend și frontend."""
    return hashlib.sha256(_canonical(schema or turn_json_schema()).encode()).hexdigest()


def parse_turn_request(payload: dict[str, Any]) -> WebTurnRequestV2:
    version = payload.get("schema_version")
    if version != TURN_SCHEMA_VERSION:
        raise ValueError(
            f"schema_version necunoscut: {version!r} (serverul vorbește {TURN_SCHEMA_VERSION!r})"
        )
    return WebTurnRequestV2.model_validate(payload)
