"""NX-230 — politica: ce categorie se redactează, către CE destinație.

Nu toate sink-urile au aceleași nevoi, iar a pretinde că au duce fie la scurgeri, fie la stricarea
funcționalității. Trei profiluri, fiecare cu o justificare pe care o pot apăra:

  • `PERSIST` — ce se scrie în `messages.body`, rezumate, facts, outbox. Redactăm tot ce e PII
    propriu-zis. **Numărul de comandă rămâne**: `check_order` are nevoie de el, iar o comandă fără
    cont are valoare mică pentru cine ar citi rândul. A-l masca aici ar rupe fluxul de comenzi ca
    să câștige aproape nimic.

  • `TELEMETRY` — analytics, loguri, traces, chei de cache, fingerprint de idempotency. Aici
    redactăm TOT, inclusiv referințele de comandă: o metrică n-are nevoie de niciun identificator,
    iar cheile de cache sunt cel mai lung-trăitor sink dintre toate.

  • `PROMPT` — ce vede modelul. Comportamentul din NX-121 rămâne neschimbat, sub același
    kill-switch (`input_pii_mask_enabled`). D6 spune că agentul principal POATE vedea query-ul
    brut; cardul ăsta nu schimbă acea decizie, doar se asigură că ce vede modelul nu devine
    automat ce se scrie pe disc.

Un sink nou se adaugă AICI, nu ad-hoc la locul apelului. Dacă cineva scrie o redactare inline
undeva în pipeline, e un bug de arhitectură: înseamnă că politica are două surse de adevăr.
"""

from __future__ import annotations

from src.privacy.contracts import CATEGORIES

# PII propriu-zis: valori care identifică o persoană sau dau acces la banii ei.
_CORE_PII: frozenset[str] = frozenset(
    {"phone", "email", "iban", "card", "cnp", "address", "secret"}
)

# Ce se redactează înainte de orice scriere durabilă.
PERSIST: frozenset[str] = _CORE_PII

# Ce se redactează înainte de orice observabilitate. Tot, fără excepție.
TELEMETRY: frozenset[str] = CATEGORIES

# Ce se maschează înainte de prompt (comportamentul NX-121, neschimbat).
PROMPT: frozenset[str] = frozenset({"phone", "email", "iban", "card"})

PROFILES: dict[str, frozenset[str]] = {
    "persist": PERSIST,
    "telemetry": TELEMETRY,
    "prompt": PROMPT,
}


def profile(name: str) -> frozenset[str]:
    """Profilul cerut. Un nume necunoscut întoarce `TELEMETRY` — cel mai STRICT.

    Fail-safe deliberat: dacă cineva greșește numele profilului, rezultatul e „am redactat prea
    mult", nu „am scris PII pe disc". Un bug de tipografie nu are voie să deschidă o scurgere.
    """
    return PROFILES.get(name, TELEMETRY)
