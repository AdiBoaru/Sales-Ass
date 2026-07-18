"""NX-180 — judge-ul LLM SECUNDAR al evaluatorului conversațional.

Măsoară ce gate-urile deterministe (`eval_gates.py`) NU pot: naturalețe, dacă a răspuns la ce a
întrebat clientul, non-repetiție, concizie, onestitate. Judge-ul dă un SCOR (1-5), NU o poartă —
`eval_run` nu-l lasă să anuleze un eșec determinist (P2, review Codex).

Reproductibilitate (DoD NX-180): promptul judge e o CONSTANTĂ versionată + hash-uită (`JUDGE_PROMPT`
+ `judge_prompt_sha256()`), înregistrată în raport. Schimbi promptul → hash nou → baseline-urile
vechi nu se compară orb cu cele noi.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

JUDGE_VERSION = "v1"

# Promptul e FIX (byte-identic între rulări). Judecă UN tur al botului în contextul conversației.
JUDGE_PROMPT = """Ești un evaluator RIGUROS al calității conversaționale a unui asistent de vânzări
pe WhatsApp/web pentru un magazin din România. Primești conversația de până acum și ULTIMUL răspuns
al botului ca EXPERIENȚĂ COMPLETĂ servită clientului: TEXT + CARDURI (produse) + eventual un
OFFER/CTA (buton/link). Evaluează DOAR ultimul răspuns al botului, în contextul conversației.

Notează pe o scală 1-5 (1 = foarte slab, 3 = acceptabil, 5 = excelent), STRICT:
- answered: a răspuns DIRECT la ce a întrebat clientul? (nu evită, nu răspunde altceva)
- natural: sună ca un consultant om, conversațional? SAU se simte șablon/robotic/repetitiv ca formă?
- non_repetitive: a evitat să repete aceeași introducere/listă/încheiere ca în tururile anterioare?
- concise: lungimea potrivită turului (scurt la o întrebare simplă, fără umplutură)?
- honest: a evitat să afirme lucruri neconfirmate / a recunoscut incertitudinea când era cazul?

Reguli:
- Fii ZGÂRCIT cu 5. Un răspuns corect dar cu structură vizibil șablonată NU ia 5 la `natural`.
- Un CARD sau un OFFER POATE fi el însuși răspunsul: la „dă-mi linkul", un offer/CTA e răspunsul
  corect (`answered` mare) chiar cu text scurt; la o cerere de produs, cardurile sunt răspunsul —
  NU marca „unanswered" un răspuns livrat prin card/offer. Evaluează experiența COMPLETĂ, nu doar
  textul. `natural`/`concise` se referă totuși la TEXT (cardurile nu-l penalizează).
- `overall` = impresia globală (1-5), nu media aritmetică.
- `note` = o singură propoziție, în română, cu motivul principal al scorului.

Răspunde DOAR cu JSON conform schemei."""

JUDGE_SCHEMA: dict[str, Any] = {
    "name": "conversation_judge",
    "strict": True,
    "schema": {
        "type": "object",
        "additionalProperties": False,
        "required": [
            "answered",
            "natural",
            "non_repetitive",
            "concise",
            "honest",
            "overall",
            "note",
        ],
        "properties": {
            "answered": {"type": "integer"},
            "natural": {"type": "integer"},
            "non_repetitive": {"type": "integer"},
            "concise": {"type": "integer"},
            "honest": {"type": "integer"},
            "overall": {"type": "integer"},
            "note": {"type": "string"},
        },
    },
}

_METRICS = ("answered", "natural", "non_repetitive", "concise", "honest", "overall")


def judge_prompt_sha256() -> str:
    """Hash-ul promptului judge + versiune + schema INTEGRALĂ → pin de reproductibilitate în raport.
    Schema completă (nu doar `required`, fix #234): o schimbare de tip/prop schimbă hash-ul."""
    h = hashlib.sha256()
    h.update(JUDGE_VERSION.encode())
    h.update(JUDGE_PROMPT.encode())
    h.update(json.dumps(JUDGE_SCHEMA, sort_keys=True, ensure_ascii=False).encode())
    return h.hexdigest()


def _clamp(v: Any) -> int:
    try:
        return max(1, min(5, int(v)))
    except (TypeError, ValueError):
        return 1


def _render_experience(
    bot_reply: str, products: list[dict[str, Any]] | None, offer: dict[str, Any] | None
) -> str:
    """Experiența COMPLETĂ servită clientului (fix #234): TEXT + CARDURI + OFFER — nu doar textul.
    Un card/offer poate FI răspunsul (ex. «dă-mi linkul»)."""
    parts = [f"TEXT: {bot_reply or '(fără text)'}"]
    prods = products or []
    if prods:
        names = ", ".join(str(p.get("name") or p.get("title") or "?") for p in prods)
        parts.append(f"CARDURI ({len(prods)}): {names}")
    else:
        parts.append("CARDURI: niciunul")
    if offer and offer.get("url"):
        parts.append(f"OFFER/CTA: {offer.get('kind') or 'link'} → {offer.get('url')}")
    return "\n".join(parts)


def build_user_message(
    transcript: list[dict[str, str]],
    bot_reply: str,
    products: list[dict[str, Any]] | None = None,
    offer: dict[str, Any] | None = None,
) -> str:
    """Transcriptul de până acum (roluri user/bot) + EXPERIENȚA COMPLETĂ a ultimului răspuns."""
    lines = []
    for m in transcript:
        who = "CLIENT" if m["role"] == "user" else "BOT"
        lines.append(f"{who}: {m['text']}")
    convo = "\n".join(lines) if lines else "(conversație nouă)"
    exp = _render_experience(bot_reply, products, offer)
    return (
        f"Conversație până acum:\n{convo}\n\n"
        f"ULTIMUL răspuns al botului (experiența COMPLETĂ servită clientului):\n{exp}"
    )


async def judge_turn(
    llm,
    transcript: list[dict[str, str]],
    bot_reply: str,
    products: list[dict[str, Any]] | None = None,
    offer: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Cheamă judge-ul pe UN tur (experiența completă: text + carduri + offer). Scoruri clampate
    1-5 + `note`. Model = agent (mini). Eroare API → scoruri None (judge indisponibil, nu 1)."""
    user = build_user_message(transcript, bot_reply, products, offer)
    try:
        raw = await llm.complete_schema(JUDGE_PROMPT, user, JUDGE_SCHEMA, model=llm.model_agent)
    except Exception as e:  # noqa: BLE001 — judge indisponibil ≠ scor 1 (nu falsifică baseline-ul)
        return {"error": type(e).__name__, **{m: None for m in _METRICS}, "note": ""}
    return {**{m: _clamp(raw.get(m)) for m in _METRICS}, "note": str(raw.get("note") or "")[:200]}
