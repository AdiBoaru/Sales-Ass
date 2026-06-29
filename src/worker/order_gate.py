"""NX-128 — poartă de comandă/retur conștientă de identitate + mesaje deterministe per-locale.

Pe web (`webchat`) nu există cont: contactul e throwaway (src/web/session.py — „fără login, fără
PII"), deci `check_order` (scoped pe `contact_id`) NU poate găsi nicio comandă, oricât de corect ar
fi numărul. Mesajul vechi „nu am găsit comanda pe acest cont" e înșelător (implică un cont căutat),
iar fluxul intră în buclă (modelul cere nr/email pe care tool-ul nu le poate folosi). Aici ținem un
predicat de identitate + mesajele deterministe, partajate de:
  • stagiul agent — scurtcircuit ÎNAINTE de bucla LLM (cost $0) pe web anonim;
  • tool-ul `check_order` — mesajul „fără comenzi" pe canalele IDENTIFICATE (telefon/chat = cont).

NX-129 va RAFINA `web_unidentified` ca web-ul cu login passthrough verificat să treacă de poartă.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from src.channels.base import IDENTIFIED_CHANNELS

if TYPE_CHECKING:
    from src.models import TurnContext


def web_unidentified(ctx: TurnContext) -> bool:
    """True dacă turul vine pe un canal ANONIM (web) fără identitate verificată → nu poate avea
    comenzi legate de contact. Canalele identificate (WhatsApp/Telegram: id-ul de canal = userul) →
    False. NX-129: web-ul cu login passthrough verificat (`ctx.verified_customer_ref`) trece de
    poartă (e identificat), deci poate ajunge la `check_order`."""
    if ctx.message.channel_kind in IDENTIFIED_CHANNELS:
        return False
    return not getattr(ctx, "verified_customer_ref", None)


_LOGIN_REQUIRED: dict[str, str] = {
    "ro": "Ca să verific o comandă sau să încep un retur, intră în contul tău pe site și revino "
    "aici — așa îți pot vedea comenzile în siguranță.",
    "en": "To check an order or start a return, please sign in to your account on the site and "
    "come back here — that lets me see your orders securely.",
    "hu": "Egy rendelés ellenőrzéséhez vagy visszaküldés indításához jelentkezz be a fiókodba az "
    "oldalon, majd térj vissza ide — így biztonságosan látom a rendeléseidet.",
}
_HANDOFF_SUFFIX: dict[str, str] = {
    "ro": " Dacă preferi, te pot pune în legătură cu un coleg.",
    "en": " If you'd prefer, I can connect you with a colleague.",
    "hu": " Ha szeretnéd, összekötlek egy kollégával.",
}
_NO_ORDERS: dict[str, str] = {
    "ro": "Nu găsesc nicio comandă pe contul tău. Dacă ai folosit alt număr sau cont, dă-mi "
    "numărul comenzii și verific din nou.",
    "en": "I can't find any orders on your account. If you used a different number or account, "
    "send me the order number and I'll check again.",
    "hu": "Nem találok rendelést a fiókodban. Ha másik számot vagy fiókot használtál, küldd el a "
    "rendelési számot, és újra megnézem.",
}


def _pick(table: dict[str, str], language: str | None) -> str:
    return table.get(language or "ro") or table["ro"]


def login_required_message(language: str | None, *, with_handoff: bool = False) -> str:
    """Mesaj determinist „loghează-te ca să-ți verific comanda" (web anonim). `with_handoff` adaugă
    oferta de operator DOAR când tenantul are `request_human` activ — nu promitem ce nu există."""
    msg = _pick(_LOGIN_REQUIRED, language)
    return msg + _pick(_HANDOFF_SUFFIX, language) if with_handoff else msg


def no_orders_message(ctx: TurnContext) -> str:
    """Mesaj `not_found` pentru canalele IDENTIFICATE (telefonul/chat-ul ESTE contul): onest („pe
    contul tău"), fără a sugera un cont căutat inexistent. Web nu ajunge aici (scurtcircuit în
    agent_stage), dar helper-ul rămâne robust dacă tool-ul e chemat direct."""
    return _pick(_NO_ORDERS, getattr(ctx, "language", None))
