"""Poarta de gating proactiv (NX-71) — decide DACĂ și CUM se trimite un mesaj proactiv.

Înainte de ORICE mesaj proactiv (AWB, back-in-stock, coș abandonat, follow-up),
sistemul trebuie să decidă determinist:
  • CONSENT — fără opt-in nu pleacă nimic (nici în fereastra 24h).
  • FEREASTRA 24h Meta — în fereastră → mesaj liber; în afara ei → DOAR template aprobat.
  • TEMPLATE — în afara ferestrei, doar un `wa_templates` cu `status='approved'` în limba cerută.

Poartă 100% cod determinist (P2): ZERO LLM/embeddings. Randarea = `str.replace`,
decizia = `if`-uri pe consent / fereastră / status. NU trimite nimic (P5): produce
DECIZIA + textul randat; NX-70 (motorul) o pune în `outbox` → dispatcher.

Contractul consumat de NX-70 (motorul proactiv):
  - `mode='free'`   (reason `ok_free`)            → trimite mesaj liber (în fereastră)
  - `mode='template'` (reason `ok_template`)      → trimite template aprobat
  - `allowed=False, reason='no_optin'`            → `proactive_jobs.status='skipped_no_optin'`
  - `allowed=False, reason='no_window_no_template'`→ `proactive_jobs.status='skipped_no_window'`

Convenția `contacts.consent` (jsonb liber, citită aici):
  `{"proactive": true, "marketing": false}` + opțional override per-kind `{"awb_update": true}`.
  Utilitare tranzacționale (awb_update, back_in_stock) cer `proactive=true`;
  marketing (abandoned_cart, follow_up) cere `marketing=true`. Un kind explicit
  `false` bate default-ul (opt-out fin); un kind explicit `true` bate default-ul (opt-in fin).

P11: lookup-ul de template filtrează pe limbă; lipsă în `locale` ≠ fallback pe altă limbă.
P12: poarta primește `Contact` (fără telefon — PII stă în channel_identities). NU loghează
telefon / body randat / valorile variabilelor (pot conține AWB/adresă).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from src.db.queries.conversations import is_in_24h_window
from src.db.queries.wa_templates import get_approved_template
from src.models import Contact

# Marketing cere consent de marketing; restul (tranzacționale) cer consent proactiv generic.
_MARKETING_KINDS = frozenset({"abandoned_cart", "follow_up"})


@dataclass(frozen=True)
class ProactiveDecision:
    """Verdictul porții. `rendered_text` e textul gata de trimis (liber SAU template
    randat — folosit ca floor de degradare pe canale fără TEMPLATE). Câmpurile `template_*`
    sunt setate DOAR pentru `mode='template'`: `template_name`/`template_language` identifică
    template-ul APROBAT la Meta, iar `template_params` sunt valorile poziționale ({{n}}) în
    ordinea din `wa_templates.variables` — exact ce-i trebuie `MetaClient.send_template`."""

    allowed: bool
    mode: str  # 'free' | 'template' | 'blocked'
    reason: str  # 'ok_free' | 'ok_template' | 'no_optin' | 'no_window_no_template'
    rendered_text: str | None = None
    template_id: str | None = None
    provider_template_id: str | None = None
    template_name: str | None = None
    template_language: str | None = None
    template_params: list[str] = field(default_factory=list)


def _has_optin(consent: dict, kind: str) -> bool:
    """True dacă există opt-in pentru acest `kind`. Override per-kind bate default-ul."""
    if consent.get(kind) is False:  # opt-out explicit per kind
        return False
    if consent.get(kind) is True:  # opt-in explicit per kind
        return True
    key = "marketing" if kind in _MARKETING_KINDS else "proactive"
    return bool(consent.get(key, False))  # default: fără opt-in => NU


def render_template(body: str, var_names: list[str], values: dict[str, str]) -> str:
    """Randează un template Meta (placeholders poziționali `{{1}}`, `{{2}}`...).

    `var_names` (din `wa_templates.variables`) e mapat pozițional la `{{n}}`; valoarea
    vine din `values[name]`. Variabilă lipsă din `values` → string gol (nu crăpăm)."""
    out = body
    for i, name in enumerate(var_names, start=1):
        val = values.get(name, "")
        out = out.replace("{{" + str(i) + "}}", str(val))
    return out


async def decide_proactive(
    conn,  # tenant_conn (bot_runtime), deja scoped pe business
    *,
    business_id: str,
    contact: Contact,
    conversation: dict,  # rândul conv (are `id`, `last_inbound_at`)
    channel_id: str,
    kind: str,  # awb_update | back_in_stock | abandoned_cart | follow_up
    locale: str,  # parte din CHEIA de template (P11)
    template_name: str,  # ce template am vrea dacă suntem în afara ferestrei
    free_text: str,  # textul liber (folosit DOAR în fereastră)
    variables: dict[str, str],  # valori pt randarea template-ului
) -> ProactiveDecision:
    """Decide determinist (consent → fereastră → template). Vezi docstring-ul modulului.

    Eroare DB la lookup-ul de template se PROPAGĂ (NX-70 marchează jobul `failed`,
    retry); poarta NU întoarce `allowed=True` tăcut la incertitudine."""
    # 1. CONSENT — fără opt-in, nu trimite NIMIC (nici în fereastră)
    if not _has_optin(contact.consent, kind):
        return ProactiveDecision(allowed=False, mode="blocked", reason="no_optin")

    # 2. FEREASTRA 24h — decisă de DB (in_24h_window), nu recalculată în Python
    in_window = await is_in_24h_window(conn, business_id, conversation["id"])
    if in_window:
        return ProactiveDecision(
            allowed=True, mode="free", reason="ok_free", rendered_text=free_text
        )

    # 3. în afara ferestrei → DOAR template approved (business_id + channel + locale)
    tmpl = await get_approved_template(
        conn, business_id, channel_id=channel_id, name=template_name, locale=locale
    )
    if tmpl is None:
        return ProactiveDecision(allowed=False, mode="blocked", reason="no_window_no_template")

    var_names = tmpl["variables"]
    text = render_template(tmpl["body"], var_names, variables)
    # Valorile poziționale ({{1}},{{2}}...) în ordinea numelor din `wa_templates.variables` —
    # exact ce trimite Meta în componenta `body` (NU textul randat, P11). Lipsă → string gol.
    params = [str(variables.get(name, "")) for name in var_names]
    return ProactiveDecision(
        allowed=True,
        mode="template",
        reason="ok_template",
        rendered_text=text,
        template_id=tmpl["id"],
        provider_template_id=tmpl["provider_template_id"],
        template_name=tmpl["name"],
        template_language=tmpl["language"],
        template_params=params,
    )
