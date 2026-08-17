"""NX-246 — ce are voie să iasă din proces spre un backend de telemetrie.

Regula pe care o impune fișierul ăsta: **telemetria nu transportă conținut.** Nu prompt, nu
completion, nu query, nu URL complet, nu argumente de tool, nu mesajul unei excepții. Nu fiindcă
„ar fi frumos", ci fiindcă un backend de observabilitate e cel mai lung-trăitor și cel mai larg
citit sink pe care îl are un sistem: e replicat, indexat, ținut luni, și îl vede toată lumea care
are acces la dashboard — inclusiv oameni care n-au dreptul să citească conversația unui client.

**Nu reimplementăm redactarea.** NX-230 (`src/privacy/`) e sursa unică de adevăr pentru „ce e PII
și cum se maschează", cu profilul `telemetry` care e cel mai strict dintre cele trei. Aici adăugăm
DOAR ce privacy-ul nu are cum să știe, fiindcă ține de forma tehnică a datelor, nu de conținut:

  • **excepții** — păstrăm lanțul de TIPURI, aruncăm mesajele. `str(exc)` e text liber generat de
    o bibliotecă terță: un `asyncpg` care pune query-ul în mesaj, un client HTTP care pune URL-ul
    cu token în query string, un `KeyError` care pune cheia. Un singur mesaj scăpat anulează tot.
  • **URL-uri** — schemă + host + FORMA căii (segmentele care arată a identificator devin `:id`).
    Query string-ul se aruncă în întregime; e locul unde trăiesc tokenurile.
  • **headere** — allowlist de NUME, valorile nu se transportă niciodată. Un `authorization`
    redactat prin regex e o cursă pe care o pierzi o dată; unul care nu e citit deloc, niciodată.
  • **argumente de tool** — cheile allowlistate, valorile reduse la TIP și mărime. „A chemat
    `search_products` cu 3 filtre" e observabilitate; „a căutat «cremă pentru [afecțiune]»" e
    conținutul conversației unui om.

Fail-safe, nu fail-open (aceeași doctrină ca `privacy/boundary.py`): dacă sanitizarea crapă,
rezultatul e un placeholder marcat, nu valoarea brută. O telemetrie incompletă e o pagubă de
observabilitate; un PII exportat e o pagubă de conformitate, și doar una se poate repara.
"""

from __future__ import annotations

import hashlib
import logging
import re
from typing import Any
from urllib.parse import urlsplit

from src.observability.contract import (
    STRUCTURAL_ATTRIBUTES,
    normalize_attribute_value,
    normalize_open_value,
)
from src.privacy.boundary import safe_for_telemetry
from src.privacy.detectors import detect

log = logging.getLogger(__name__)

#: Cât din orice text liber lăsăm să treacă DUPĂ redactare. Un span cu 4KB de text e un span care
#: transportă conținut, oricât de redactat ar fi.
MAX_TEXT_CHARS = 120

#: Ce se pune când nu putem garanta că valoarea e sigură.
REDACTED = "[redactat]"

#: Adâncimea maximă a unui lanț de excepții (`__cause__`/`__context__`). Un lanț circular sau
#: patologic de lung nu are voie să transforme o eroare într-o buclă în calea de logare.
MAX_EXC_DEPTH = 5

#: Numele de headere pe care le raportăm ca PREZENȚĂ (valoarea nu se transportă NICIODATĂ).
SAFE_HEADER_NAMES: frozenset[str] = frozenset(
    {"content-type", "content-length", "user-agent", "accept", "accept-encoding", "origin"}
)

#: Headerele a căror simplă PREZENȚĂ o raportăm, dar pe care nu le numim în clar în atribute.
_SENSITIVE_HEADERS: frozenset[str] = frozenset(
    {"authorization", "cookie", "set-cookie", "x-api-key", "x-signature", "proxy-authorization"}
)

# Segment de cale care arată a identificator: UUID, hex lung, numeric, slug cu cifre multe.
_UUID = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
_HEXISH = re.compile(r"^[0-9a-fA-F]{16,}$")
_NUMERIC = re.compile(r"^\d+$")
_TYPE_NAME = re.compile(r"[^A-Za-z0-9]+")


def safe_text(value: Any, *, limit: int = MAX_TEXT_CHARS) -> str:
    """Orice text destinat telemetriei: redactat cu profilul cel mai strict, apoi TRUNCHIAT.

    Trunchierea nu e cosmetică. Redactarea prinde formele pe care le știm (telefon, email, IBAN,
    CNP, adresă, secret); nu poate prinde o propoziție în care clientul își spune povestea. Limita
    face ca, în cel mai rău caz, ce scapă să fie un fragment, nu un transcript.
    """
    if value is None:
        return ""
    try:
        text = value if isinstance(value, str) else str(value)
        cleaned = safe_for_telemetry(text)
    except Exception:  # noqa: BLE001 — fail-safe: vezi docstring-ul modulului
        log.warning("observability sanitize: safe_text a eșuat")
        return REDACTED
    cleaned = cleaned.strip()
    return cleaned[:limit] if len(cleaned) > limit else cleaned


def safe_error_code(exc: BaseException | type[BaseException] | str | None) -> str:
    """Cod de eroare STABIL și low-cardinality, derivat din TIP — niciodată din mesaj.

    `TimeoutError` → `timeout_error`; `asyncpg.UniqueViolationError` → `unique_violation_error`.
    Ăsta e singurul lucru dintr-o excepție care are voie să devină etichetă: e finit (câte tipuri
    de excepții există în dependențe), stabil între rulări și nu poate conține date de client.
    """
    if exc is None:
        return "none"
    if isinstance(exc, str):
        name = exc
    else:
        cls = exc if isinstance(exc, type) else type(exc)
        name = cls.__name__
    snake = _TYPE_NAME.sub("_", name).strip("_")
    snake = re.sub(r"(?<!^)(?=[A-Z])", "_", snake).lower()
    snake = re.sub(r"_+", "_", snake).strip("_")
    return snake[:48] or "unknown_error"


def exception_chain(exc: BaseException | None, *, depth: int = MAX_EXC_DEPTH) -> str:
    """Lanțul de excepții ca TIPURI: `client_error<timeout_error<os_error`.

    Ce se pierde: mesajele. Ce rămâne: exact informația de diagnostic care contează într-un
    dashboard — „timeoutul de rețea a devenit eroare de client", nu care URL a expirat.
    """
    parts: list[str] = []
    seen: set[int] = set()
    cur: BaseException | None = exc
    while cur is not None and len(parts) < depth:
        if id(cur) in seen:  # lanț circular (se poate construi manual) — oprim, nu buclăm
            break
        seen.add(id(cur))
        parts.append(safe_error_code(cur))
        cur = cur.__cause__ or cur.__context__
    return "<".join(parts)


def safe_url(url: str | None) -> str:
    """URL → `scheme://host/forma/caii`. Query string ARUNCAT, segmentele-identificator → `:id`.

    Un URL complet e a doua cea mai frecventă scurgere din telemetrie după mesajele de excepție:
    `?token=`, `?email=`, `/orders/12345`. Forma căii răspunde la „ce endpoint a fost lent" fără
    să răspundă la „al cui".
    """
    if not url:
        return ""
    try:
        parts = urlsplit(url)
        segments = []
        for seg in parts.path.split("/"):
            if not seg:
                continue
            if _UUID.match(seg) or _HEXISH.match(seg) or _NUMERIC.match(seg):
                segments.append(":id")
            elif len(seg) > 48:
                segments.append(":seg")
            else:
                segments.append(safe_for_telemetry(seg)[:48])
        host = parts.hostname or ""
        path = "/" + "/".join(segments) if segments else "/"
        return f"{parts.scheme}://{host}{path}" if parts.scheme else f"{host}{path}"
    except Exception:  # noqa: BLE001 — fail-safe
        log.warning("observability sanitize: safe_url a eșuat")
        return REDACTED


def safe_headers(headers: dict[str, Any] | None) -> dict[str, str]:
    """Headere → prezență, nu conținut.

    Pentru cele allowlistate raportăm o valoare redactată+trunchiată (`content-type` e util);
    pentru cele sensibile raportăm DOAR că există (`present`). Nicio valoare de autorizare nu
    trece prin regex — nu o citim deloc, ceea ce e singura garanție care nu se erodează.
    """
    if not headers:
        return {}
    out: dict[str, str] = {}
    for raw_key, raw_val in headers.items():
        key = str(raw_key).lower()
        if key in _SENSITIVE_HEADERS:
            out[key] = "present"
        elif key in SAFE_HEADER_NAMES:
            out[key] = safe_text(raw_val, limit=48)
    return out


def safe_args(
    args: dict[str, Any] | None, *, allow: frozenset[str] | None = None
) -> dict[str, str]:
    """Argumente de tool → FORMA lor: tip și mărime, nu valori.

    `{"category": "creme", "budget_max": 120}` devine `{"category": "str", "budget_max": "int"}`.
    Câte filtre a folosit modelul e o întrebare de observabilitate; ce a căutat clientul nu e.
    `allow` restrânge și cheile — un tool care primește un câmp nou nu îl publică automat.
    """
    if not args:
        return {}
    out: dict[str, str] = {}
    for key, value in args.items():
        skey = str(key)
        if allow is not None and skey not in allow:
            continue
        if not re.match(r"^[A-Za-z_][A-Za-z0-9_]{0,31}$", skey):
            continue
        if isinstance(value, bool):
            out[skey] = "bool"
        elif isinstance(value, (int, float)):
            out[skey] = type(value).__name__
        elif isinstance(value, str):
            out[skey] = "str"
        elif isinstance(value, (list, tuple, set)):
            out[skey] = f"list[{len(value)}]"
        elif isinstance(value, dict):
            out[skey] = f"dict[{len(value)}]"
        elif value is None:
            out[skey] = "none"
        else:
            out[skey] = "opaque"
    return out


def safe_label_value(value: Any) -> str | None:
    """Valoare de etichetă/atribut: formă de identificator **ȘI** fără secrete/PII.

    `None` = respinsă.

    De ce nu e de ajuns forma: `sk-proj-AbCdEf...` e un identificator perfect valid ca formă —
    alfanumeric, fără spații, lungime rezonabilă. La fel un IBAN, la fel un CNP. Un `model_id`
    otrăvit cu o cheie API ar trece orice verificare sintactică și ar ajunge într-un backend
    replicat, indexat și citit de toată echipa.

    De aceea poarta are DOUĂ etaje: forma (cardinalitate) și conținutul (privacy, prin NX-230).
    Ambele au fost adăugate după ce testul de canary a găsit exact aceste două găuri.
    """
    shaped = normalize_open_value(value)
    if shaped is None:
        return None
    return None if detect(shaped) else shaped


def safe_attribute_value(key: str, value: Any) -> str | int | float | bool | None:
    """Ca `safe_label_value`, dar pentru atribute de span (unde există și valori numerice și
    atributele generate de noi, ex. `exception_chain`)."""
    shaped = normalize_attribute_value(key, value)
    if not isinstance(shaped, str):
        return shaped
    if key in STRUCTURAL_ATTRIBUTES:
        # Deja validat structural; scanarea ar produce doar fals-pozitive (vezi contract.py).
        return shaped
    return None if detect(shaped) else shaped


def correlation_ref(value: str | None, *, salt: str = "") -> str:
    """ID intern → referință de corelare NE-inversabilă, scurtă.

    Folosită pentru `conversation_ref`: într-un trace vrei să poți lega două ture ale aceleiași
    conversații, dar nu vrei ca backendul de telemetrie să devină un index al conversațiilor
    tenanților. Hash trunchiat: suficient pentru grupare, inutil pentru enumerare.
    (Aceeași unealtă ca `session_ref_hash` din NX-232, cu alt scop și alt sink.)
    """
    if not value:
        return ""
    return hashlib.sha256(f"{salt}:{value}".encode()).hexdigest()[:16]
