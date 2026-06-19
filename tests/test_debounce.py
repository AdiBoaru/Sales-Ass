"""Teste unit pentru debounce (R1) — coalescing mesaje rapide, timing cu delay mic."""

import asyncio

from src.worker.debounce import Debouncer


def _ev(sender: str, body: str, mid: str, *, account="bot1") -> dict:
    return {
        "channel_kind": "telegram",
        "channel_account_id": account,
        "sender_external_id": sender,
        "body": body,
        "provider_msg_id": mid,
    }


async def test_coalesces_rapid_messages_into_one_turn():
    processed: list[dict] = []

    async def proc(ev):
        processed.append(ev)

    d = Debouncer(proc, delay=0.05)
    await d.add(_ev("u", "caut o cremă", "1"))
    await d.add(_ev("u", "pentru ten uscat", "2"))
    await asyncio.sleep(0.12)  # > delay → un singur flush

    assert len(processed) == 1
    assert processed[0]["body"] == "caut o cremă\npentru ten uscat"
    assert processed[0]["provider_msg_id"] == "2"  # ultimul mesaj


async def test_separate_senders_not_merged():
    processed: list[dict] = []

    async def proc(ev):
        processed.append(ev)

    d = Debouncer(proc, delay=0.05)
    await d.add(_ev("u1", "a", "1"))
    await d.add(_ev("u2", "b", "2"))
    await asyncio.sleep(0.12)

    assert len(processed) == 2  # expeditori diferiți → tururi separate


async def test_new_message_resets_timer():
    processed: list[dict] = []

    async def proc(ev):
        processed.append(ev)

    d = Debouncer(proc, delay=0.1)
    await d.add(_ev("u", "a", "1"))
    await asyncio.sleep(0.06)  # < delay
    await d.add(_ev("u", "b", "2"))  # resetează timerul
    await asyncio.sleep(0.06)  # doar 0.06 de la al 2-lea → încă nu s-a procesat
    assert processed == []
    await asyncio.sleep(0.08)  # acum > delay de la al 2-lea
    assert len(processed) == 1
    assert processed[0]["body"] == "a\nb"


async def test_single_message_passes_through():
    processed: list[dict] = []

    async def proc(ev):
        processed.append(ev)

    d = Debouncer(proc, delay=0.03)
    await d.add(_ev("u", "salut", "1"))
    await asyncio.sleep(0.08)
    assert len(processed) == 1
    assert processed[0]["body"] == "salut"


# --- NX-87: ACK după flush (durabilitate) ------------------------------------


async def test_acks_msg_ids_after_successful_flush():
    acked: list[list[str]] = []

    async def proc(ev):
        pass

    async def ack(ids):
        acked.append(list(ids))

    d = Debouncer(proc, delay=0.05, ack=ack)
    await d.add(_ev("u", "a", "1"), "stream-1")
    await d.add(_ev("u", "b", "2"), "stream-2")
    await asyncio.sleep(0.12)
    assert acked == [["stream-1", "stream-2"]]  # toate msg_id-urile lotului, DUPĂ flush reușit


async def test_no_ack_when_process_fails():
    acked: list = []

    async def proc(ev):
        raise RuntimeError("flush boom")

    async def ack(ids):
        acked.append(ids)

    d = Debouncer(proc, delay=0.05, ack=ack)
    await d.add(_ev("u", "a", "1"), "stream-1")
    await asyncio.sleep(0.12)
    assert acked == []  # flush eșuat → fără ACK (mesajul rămâne pending, nu pierdut)
