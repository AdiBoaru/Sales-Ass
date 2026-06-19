"""Client pentru trimiterea mesajelor prin Meta WhatsApp Cloud API.

Folosit DOAR de dispatcher (singurul care trimite — principiul 5). Webhook-ul
parsează inbound (webhook/meta.py); ăsta e capătul de OUTBOUND.

`httpx.AsyncClient`-ul se injectează → testele pasează unul cu MockTransport,
zero apeluri reale în CI. Erorile HTTP se propagă (dispatcher-ul le prinde și
programează retry cu backoff).
"""

import httpx


class MetaSendError(RuntimeError):
    """Răspuns Meta fără un message id utilizabil (payload neașteptat)."""


class MetaClient:
    """Wrapper subțire peste Graph API /{phone_number_id}/messages."""

    def __init__(
        self,
        http: httpx.AsyncClient,
        token: str,
        *,
        base_url: str = "https://graph.facebook.com",
        version: str = "v21.0",
    ) -> None:
        self._http = http
        self._token = token
        self._base = f"{base_url.rstrip('/')}/{version}"

    async def send_text(self, account_id: str, to: str, text: str) -> str:
        """Trimite un mesaj text. Întoarce wamid-ul (provider_msg_id) de la Meta.

        Implementează `ChannelSender` (NX-60): `account_id` = numărul EXPEDITOR
        (phone_number_id), `to` = destinatarul (wa_id). Ridică la status HTTP de
        eroare (raise_for_status) sau dacă răspunsul nu conține un message id."""
        resp = await self._http.post(
            f"{self._base}/{account_id}/messages",
            headers={"Authorization": f"Bearer {self._token}"},
            json={
                "messaging_product": "whatsapp",
                "recipient_type": "individual",
                "to": to,
                "type": "text",
                "text": {"body": text},
            },
        )
        resp.raise_for_status()
        data = resp.json()
        try:
            return data["messages"][0]["id"]
        except (KeyError, IndexError, TypeError) as e:
            raise MetaSendError(f"răspuns Meta fără message id: {data}") from e

    async def mark_typing(self, account_id: str, to: str, provider_msg_id: str | None) -> None:
        """NX-90: marchează inbound-ul ca citit + arată „typing…" (Meta unește read + typing
        într-un singur call). Bula dispare automat la ~25s sau la primul mesaj outbound. Necesită
        wamid-ul inbound (`provider_msg_id`); fără el e no-op (Meta cere message_id). Best-effort —
        ridică la eroare HTTP, caller-ul (`_safe_typing`) prinde și ignoră (P6). `to` ignorat
        (Meta țintește pe message_id)."""
        if not provider_msg_id:
            return
        resp = await self._http.post(
            f"{self._base}/{account_id}/messages",
            headers={"Authorization": f"Bearer {self._token}"},
            json={
                "messaging_product": "whatsapp",
                "status": "read",
                "message_id": provider_msg_id,
                "typing_indicator": {"type": "text"},
            },
        )
        resp.raise_for_status()

    async def fetch_media(
        self, account_id: str, media_id: str, *, max_bytes: int | None = None
    ) -> tuple[bytes, str]:
        """Descarcă o media inbound (poză/voce) → `(bytes, mime)`. Implementează `MediaFetcher`
        (NX-76): folosit de Gates pt Vision/STT, NU de dispatcher.

        Flux Graph în 2 hop-uri: `GET /{media_id}` (cu Bearer) → metadata (`url` semnat host
        lookaside, `mime_type`, `file_size`) → `GET url` (tot cu Bearer) → bytes. `account_id` e
        informativ (token-ul autorizează). `max_bytes`: dacă metadata raportează `file_size` peste
        prag, ridicăm ÎNAINTE de a descărca binarul (nu bufferiza zeci de MB pe un VPS mic).
        Ridică la status HTTP de eroare / prea mare — caller-ul (gate) degradează fail-soft."""
        meta = await self._http.get(
            f"{self._base}/{media_id}",
            headers={"Authorization": f"Bearer {self._token}"},
        )
        meta.raise_for_status()
        info = meta.json()
        url = info["url"]  # KeyError → fail-soft în gate (try/except)
        mime = info.get("mime_type") or "application/octet-stream"
        size = info.get("file_size")
        if max_bytes is not None and isinstance(size, int) and size > max_bytes:
            raise MetaSendError(f"media prea mare: {size} > {max_bytes}")
        blob = await self._http.get(url, headers={"Authorization": f"Bearer {self._token}"})
        blob.raise_for_status()
        return blob.content, mime
