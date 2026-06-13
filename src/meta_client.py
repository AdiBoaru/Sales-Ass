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
