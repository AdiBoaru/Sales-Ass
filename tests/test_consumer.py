"""NX-90 — typing instant pe inbound (`_safe_typing`). Fără DB/rețea: registru + sender fals."""

from src.channels.base import ChannelSenderRegistry
from src.worker.consumer import _safe_typing

EVENT = {
    "channel_kind": "whatsapp",
    "channel_account_id": "PNID",
    "sender_external_id": "40712345678",
    "provider_msg_id": "wamid.IN1",
}


class _TypingSender:
    def __init__(self):
        self.calls = []

    async def send_text(self, account_id, to, text):  # parte din ChannelSender
        return "x"

    async def mark_typing(self, account_id, to, provider_msg_id):
        self.calls.append((account_id, to, provider_msg_id))


class _NoTypingSender:
    async def send_text(self, account_id, to, text):
        return "x"


def _registry(sender) -> ChannelSenderRegistry:
    reg = ChannelSenderRegistry()
    reg.register("whatsapp", sender)
    return reg


async def test_safe_typing_fires_with_envelope_args():
    s = _TypingSender()
    await _safe_typing(_registry(s), EVENT)
    assert s.calls == [("PNID", "40712345678", "wamid.IN1")]


async def test_safe_typing_skips_channel_without_mark_typing():
    s = _NoTypingSender()
    await _safe_typing(_registry(s), EVENT)  # hasattr False → skip tăcut, fără eroare


async def test_safe_typing_noop_when_registry_none():
    await _safe_typing(None, EVENT)  # compat dev/test — fără registru


async def test_safe_typing_noop_for_unknown_channel():
    s = _TypingSender()
    reg = _registry(s)  # doar whatsapp
    await _safe_typing(reg, {**EVENT, "channel_kind": "telegram"})
    assert s.calls == []  # canal neînregistrat → niciun apel


async def test_safe_typing_swallows_transport_error():
    class _Boom:
        async def send_text(self, account_id, to, text):
            return "x"

        async def mark_typing(self, account_id, to, provider_msg_id):
            raise RuntimeError("api down")

    await _safe_typing(_registry(_Boom()), EVENT)  # NU propagă (P6)
