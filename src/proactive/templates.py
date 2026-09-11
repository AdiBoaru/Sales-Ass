"""Poarta de gating proactiv (NX-71) — decide DACĂ pleacă un mesaj proactiv.

Înainte de ORICE mesaj proactiv (AWB, back-in-stock, coș abandonat, follow-up),
sistemul trebuie să decidă determinist:
  • CONSENT — fără opt-in nu pleacă nimic.

NX-289: poarta avea DOUĂ etaje în plus, ambele pur WhatsApp — fereastra de 24h impusă de Meta
și template-urile aprobate (`wa_templates`) pentru mesajele din afara ei. Odată cu scoaterea
canalului, etajele au dispărut: pe `webchat` nu există nici fereastră impusă de platformă, nici
noțiunea de mesaj pre-aprobat. Rămâne consentul, care e o regulă a NOASTRĂ, nu a unui provider.

Poartă 100% cod determinist (P2): ZERO LLM/embeddings, decizia = `if`-uri pe consent. NU trimite
nimic (P5): produce DECIZIA + textul; NX-70 (motorul) o pune în `outbox` → dispatcher.

Contractul consumat de NX-70 (motorul proactiv):
  - `mode='free'` (reason `ok_free`)   → trimite mesajul
  - `allowed=False, reason='no_optin'` → `proactive_jobs.status='skipped_no_optin'`

Convenția `contacts.consent` (jsonb liber, citită aici):
  `{"proactive": true, "marketing": false}` + opțional override per-kind `{"awb_update": true}`.
  Utilitare tranzacționale (awb_update, back_in_stock) cer `proactive=true`;
  marketing (abandoned_cart, follow_up) cere `marketing=true`. Un kind explicit
  `false` bate default-ul (opt-out fin); un kind explicit `true` bate default-ul (opt-in fin).

P12: poarta primește `Contact` (fără telefon — PII stă în channel_identities). NU loghează
body-ul randat / valorile variabilelor (pot conține AWB/adresă).
"""

from __future__ import annotations

from dataclasses import dataclass

from src.models import Contact

# Marketing cere consent de marketing; restul (tranzacționale) cer consent proactiv generic.
_MARKETING_KINDS = frozenset({"abandoned_cart", "follow_up"})


@dataclass(frozen=True)
class ProactiveDecision:
    """Verdictul porții. `rendered_text` e textul gata de trimis."""

    allowed: bool
    mode: str  # 'free' | 'blocked'
    reason: str  # 'ok_free' | 'no_optin'
    rendered_text: str | None = None


def _has_optin(consent: dict, kind: str) -> bool:
    """True dacă există opt-in pentru acest `kind`. Override per-kind bate default-ul."""
    if consent.get(kind) is False:  # opt-out explicit per kind
        return False
    if consent.get(kind) is True:  # opt-in explicit per kind
        return True
    key = "marketing" if kind in _MARKETING_KINDS else "proactive"
    return bool(consent.get(key, False))  # default: fără opt-in => NU


def decide_proactive(
    *,
    contact: Contact,
    kind: str,  # awb_update | back_in_stock | abandoned_cart | follow_up
    free_text: str,
) -> ProactiveDecision:
    """Decide determinist pe consent. Vezi docstring-ul modulului.

    NX-289: funcția nu mai e `async` și nu mai primește `conn` — singura interogare pe care o
    făcea era lookup-ul de template aprobat. Semnătura o spune: poarta nu mai atinge DB-ul, deci
    nici nu mai poate eșua din cauza lui."""
    if not _has_optin(contact.consent, kind):
        return ProactiveDecision(allowed=False, mode="blocked", reason="no_optin")
    return ProactiveDecision(allowed=True, mode="free", reason="ok_free", rendered_text=free_text)
