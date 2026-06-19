"""Client Telegram Bot API (httpx injectabil → mock în teste, zero apeluri reale în CI).

Acoperă strict ce ne trebuie pentru canalul de TEST:
  • get_updates  — long polling inbound (NX-61), fără webhook/HTTPS
  • send_text    — outbound (NX-62); implementează ChannelSender
  • get_me       — bot id, pentru onboarding/seed (NX-63)

Telegram întoarce mereu `{"ok": bool, "result": ...}`. La `ok=false` sau status
HTTP de eroare ridicăm — apelantul (poller/dispatcher) decide retry/skip.
"""

import httpx


def _short(name: str, limit: int = 30) -> str:
    """Scurtează numele pentru eticheta unui buton (Telegram trunchiază urât numele lungi)."""
    name = name.strip()
    return name if len(name) <= limit else name[: limit - 1].rstrip() + "…"


# Poză de rezervă când produsul n-are imagine (R2; placehold .png, ca W1).
_FALLBACK_IMG = "https://placehold.co/600x600/png?text=Produs"


def _carousel_caption(product: dict, index: int, total: int) -> str:
    """Textul de sub poza din carusel: nume + preț + poziția în set."""
    name = product.get("name") or "Produs"
    price = float(product["price"]) if product.get("price") is not None else None
    price_line = f"\n💰 {price:.2f} lei" if price is not None else ""
    return f"{name}{price_line}\n{index + 1}/{total}"


def _carousel_keyboard(product: dict, index: int, total: int) -> dict:
    """`◀ 🛒 ▶` — ◀/▶ navighează (callback cu indexul ȚINTĂ → navigare stateless),
    🛒 = url-button spre pagina produsului (ca W1, fără backend de coș). Butoanele
    din afara limitelor sunt omise (clamp la capete, fără wrap-around)."""
    row: list[dict] = []
    if index > 0:
        row.append({"text": "◀", "callback_data": f"car:nav:{index - 1}"})
    if product.get("url"):
        row.append({"text": "🛒 Vezi produsul", "url": product["url"]})
    if index < total - 1:
        row.append({"text": "▶", "callback_data": f"car:nav:{index + 1}"})
    return {"inline_keyboard": [row]} if row else {"inline_keyboard": []}


class TelegramError(RuntimeError):
    """Răspuns Telegram cu ok=false sau payload neașteptat."""


class TelegramClient:
    """Wrapper subțire peste https://api.telegram.org/bot{token}/{method}."""

    def __init__(
        self,
        http: httpx.AsyncClient,
        token: str,
        *,
        base_url: str = "https://api.telegram.org",
    ) -> None:
        self._http = http
        self._base = f"{base_url.rstrip('/')}/bot{token}"

    async def _call(self, method: str, payload: dict, *, timeout: float | None = None) -> object:
        resp = await self._http.post(f"{self._base}/{method}", json=payload, timeout=timeout)
        resp.raise_for_status()
        data = resp.json()
        if not data.get("ok"):
            raise TelegramError(f"{method}: {data}")
        return data["result"]

    async def get_updates(
        self,
        offset: int,
        *,
        timeout: int = 30,
        limit: int = 100,
    ) -> list[dict]:
        """Long polling: întoarce update-urile cu id >= offset. `timeout` ține
        conexiunea deschisă până la atâtea secunde dacă nu există update-uri."""
        result = await self._call(
            "getUpdates",
            {"offset": offset, "timeout": timeout, "limit": limit},
            # httpx trebuie să aștepte mai mult decât long-poll-ul Telegram
            timeout=timeout + 10,
        )
        return result if isinstance(result, list) else []

    async def send_text(self, account_id: str, to: str, text: str) -> str:
        """Implementează ChannelSender: trimite text → întoarce message_id (str).
        `account_id` (bot id) e informativ — tokenul e în URL. `to` = chat_id."""
        result = await self._call("sendMessage", {"chat_id": to, "text": text})
        try:
            return str(result["message_id"])
        except (KeyError, TypeError) as e:
            raise TelegramError(f"sendMessage fără message_id: {result}") from e

    async def mark_typing(self, account_id: str, to: str, provider_msg_id: str | None) -> None:
        """NX-90: `sendChatAction` cu `action=typing` → bula „...scrie" (~5s, suficient pt un tur
        normal; fără keep-alive în P1). `provider_msg_id` ignorat (Bot API n-are read receipts).
        Best-effort: ridică la eroare, caller-ul (`_safe_typing`) prinde și ignoră (P6)."""
        await self._call("sendChatAction", {"chat_id": to, "action": "typing"})

    async def send_products(self, account_id: str, to: str, text: str, products: list[dict]) -> str:
        """Carduri compacte (W1): UN singur mesaj — textul de recomandare + un buton
        inline per produs (nume scurt + preț → link). Pattern „listă tappabilă",
        compact pe telefon (fără poze mari). Întoarce message_id-ul mesajului.

        Produsele fără URL nu pot avea buton → apar doar în text (deja le conține)."""
        rows = [
            [{"text": f"🛍️ {_short(p['name'])} — {float(p['price']):.2f} lei", "url": p["url"]}]
            for p in products
            if p.get("url")
        ]
        payload: dict = {"chat_id": to, "text": text or "Recomandările mele:"}
        if rows:
            payload["reply_markup"] = {"inline_keyboard": rows}
        result = await self._call("sendMessage", payload)
        try:
            return str(result["message_id"])
        except (KeyError, TypeError) as e:
            raise TelegramError(f"sendMessage fără message_id: {result}") from e

    async def send_rich(self, account_id: str, to: str, payload: dict) -> str:
        """Recomandare bogată (model iZi): UN mesaj cu textul complet (intro + carduri +
        pick + educație + disclaimer) + butoane-link inline per produs; apoi, opțional, un
        al doilea mesaj cu chips ca reply-keyboard. Repară drop-ul de text al caruselului —
        textul recomandării ajunge MEREU la client. Întoarce message_id-ul primului mesaj.

        Chips = reply-keyboard: tap-ul trimite `label` ca mesaj normal → reintră în pipeline
        ca tur NOU (fără callback/recursie). `account_id` informativ (token în URL)."""
        rich = payload.get("rich") or {}
        # Butoanele-link cer URL absolut http(s) — altfel Telegram respinge ÎNTREG mesajul.
        # Fără scheme valid → fără buton (textul bogat ajunge oricum la client).
        rows = [
            [{"text": f"🛍️ {_short(it['name'])} — {float(it['price']):.2f} lei", "url": it["url"]}]
            for it in (rich.get("items") or [])
            if isinstance(it.get("url"), str) and it["url"].startswith(("http://", "https://"))
        ]
        msg: dict = {"chat_id": to, "text": payload.get("text") or "Recomandările mele:"}
        if rows:
            msg["reply_markup"] = {"inline_keyboard": rows}
        result = await self._call("sendMessage", msg)
        try:
            message_id = str(result["message_id"])
        except (KeyError, TypeError) as e:
            raise TelegramError(f"sendMessage fără message_id: {result}") from e

        chips = rich.get("chips") or []
        if chips:
            await self._call(
                "sendMessage",
                {
                    "chat_id": to,
                    "text": "Pot continua cu:",
                    "reply_markup": {
                        "keyboard": [[{"text": c["label"]}] for c in chips],
                        "resize_keyboard": True,
                        "one_time_keyboard": True,
                    },
                },
            )
        return message_id

    async def send_carousel_card(
        self, account_id: str, to: str, products: list[dict], index: int = 0
    ) -> str:
        """Carusel (R2): UN card de produs (poză + nume + preț) cu butoane ◀🛒▶.
        Trimite `sendPhoto` pentru `products[index]`; navigarea ulterioară editează
        ACEST mesaj (vezi `edit_message_media`). Întoarce message_id-ul cardului."""
        product = products[index]
        result = await self._call(
            "sendPhoto",
            {
                "chat_id": to,
                "photo": product.get("image") or _FALLBACK_IMG,
                "caption": _carousel_caption(product, index, len(products)),
                "reply_markup": _carousel_keyboard(product, index, len(products)),
            },
        )
        try:
            return str(result["message_id"])
        except (KeyError, TypeError) as e:
            raise TelegramError(f"sendPhoto fără message_id: {result}") from e

    async def edit_message_media(
        self, account_id: str, chat_id: str, message_id: str, products: list[dict], index: int
    ) -> str:
        """Editează cardul existent la `products[index]` (`editMessageMedia` —
        schimbă poza + caption + butoane în loc, fără mesaj nou). Întoarce message_id."""
        product = products[index]
        await self._call(
            "editMessageMedia",
            {
                "chat_id": chat_id,
                "message_id": int(message_id),
                "media": {
                    "type": "photo",
                    "media": product.get("image") or _FALLBACK_IMG,
                    "caption": _carousel_caption(product, index, len(products)),
                },
                "reply_markup": _carousel_keyboard(product, index, len(products)),
            },
        )
        return str(message_id)

    async def answer_callback_query(self, callback_id: str) -> None:
        """Oprește spinner-ul de pe buton (Telegram îl arată ~15s). ACK de transport."""
        await self._call("answerCallbackQuery", {"callback_query_id": callback_id})

    async def get_me(self) -> dict:
        """Info despre bot (id, username) — pentru seed-ul canalului (NX-63)."""
        result = await self._call("getMe", {})
        return result if isinstance(result, dict) else {}
