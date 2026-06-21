"""NX-90 — typing instant pe inbound (`_safe_typing`). Fără DB/rețea: registru + sender fals.
NX-115: gardarea e pe capabilitatea TYPING declarată (nu `hasattr`)."""

from src.channels.base import Capability, ChannelSenderRegistry
from src.worker.consumer import _safe_typing

EVENT = {
    "channel_kind": "whatsapp",
    "channel_account_id": "PNID",
    "sender_external_id": "40712345678",
    "provider_msg_id": "wamid.IN1",
}


class _TypingSender:
    capabilities = frozenset({Capability.TEXT, Capability.TYPING})

    def __init__(self):
        self.calls = []

    async def send_text(self, account_id, to, text):  # parte din ChannelSender
        return "x"

    async def mark_typing(self, account_id, to, provider_msg_id):
        self.calls.append((account_id, to, provider_msg_id))


class _NoTypingSender:
    capabilities = frozenset({Capability.TEXT})  # fără TYPING → skip

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


async def test_safe_typing_skips_channel_without_typing_cap():
    s = _NoTypingSender()
    await _safe_typing(_registry(s), EVENT)  # fără capabilitatea TYPING → skip tăcut, fără eroare


async def test_safe_typing_noop_when_registry_none():
    await _safe_typing(None, EVENT)  # compat dev/test — fără registru


async def test_safe_typing_noop_when_channel_kind_missing():
    # NX-115: fără default „whatsapp" — event fără channel_kind NU mai trimite typing pe WhatsApp.
    s = _TypingSender()
    reg = _registry(s)
    await _safe_typing(reg, {k: v for k, v in EVENT.items() if k != "channel_kind"})
    assert s.calls == []


async def test_safe_typing_noop_for_unknown_channel():
    s = _TypingSender()
    reg = _registry(s)  # doar whatsapp
    await _safe_typing(reg, {**EVENT, "channel_kind": "telegram"})
    assert s.calls == []  # canal neînregistrat → niciun apel


async def test_safe_typing_swallows_transport_error():
    class _Boom:
        capabilities = frozenset({Capability.TEXT, Capability.TYPING})

        async def send_text(self, account_id, to, text):
            return "x"

        async def mark_typing(self, account_id, to, provider_msg_id):
            raise RuntimeError("api down")

    await _safe_typing(_registry(_Boom()), EVENT)  # NU propagă (P6)
