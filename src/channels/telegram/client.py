"""Client Telegram Bot API (httpx injectabil → mock în teste, zero apeluri reale în CI).

Acoperă strict ce ne trebuie pentru canalul de TEST:
  • get_updates  — long polling inbound (NX-61), fără webhook/HTTPS
  • send_text    — outbound (NX-62); implementează ChannelSender
  • get_me       — bot id, pentru onboarding/seed (NX-63)

Telegram întoarce mereu `{"ok": bool, "result": ...}`. La `ok=false` sau status
HTTP de eroare ridicăm — apelantul (poller/dispatcher) decide retry/skip.
"""

import httpx


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

    async def get_me(self) -> dict:
        """Info despre bot (id, username) — pentru seed-ul canalului (NX-63)."""
        result = await self._call("getMe", {})
        return result if isinstance(result, dict) else {}
