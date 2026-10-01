"""NX-366 — replay-ul pe pipeline-ul REAL: DB real, toate stagiile, `/web/chat` in-process, 0 $.

Un furnizor fals joacă modelul (o rundă de unelte care caută, apoi text; `{}` pe orice schemă),
iar turul trece prin `web_chat` cu captura aprinsă, exact ca widgetul. Apoi fiecare rând scris în
`conversation_traces` se rejoacă pe același cod: ce vede clientul trebuie să fie identic, iar
intrarea fiecărui apel de model identică byte cu byte (o intrare diferită la același cod ar fi
nedeterminism, adică instrumentul ar minți). Vizitatorul e `web_audit_*`, purjat la final.

    NX_TESTS_READ_ENV_FILE=1 pytest tests/test_nx366_replay_db.py -m integration -q
"""

from __future__ import annotations

import json
from types import SimpleNamespace as NS
from typing import Any

import pytest
from openai.types import ModerationCreateResponse
from openai.types.chat import ChatCompletion

pytestmark = pytest.mark.integration

TOKEN = "pub_b738dd1aa2ff2e0535b491792cc789d9"
BIZ = "99fe1292-f9ed-469e-8183-f994ea5b59c0"
MESSAGES = ["vreau o crema de hidratare", "am tenul uscat", "mai arata-mi"]


def _completion(content: str | None = None, tool: tuple[str, dict] | None = None) -> ChatCompletion:
    msg: dict[str, Any] = {"role": "assistant", "content": content}
    if tool:
        msg["tool_calls"] = [
            {
                "id": "call_1",
                "type": "function",
                "function": {"name": tool[0], "arguments": json.dumps(tool[1])},
            }
        ]
    return ChatCompletion.model_validate(
        {
            "id": "chatcmpl-replay",
            "object": "chat.completion",
            "created": 0,
            "model": "gpt-6-luna",
            "choices": [{"index": 0, "finish_reason": "stop", "message": msg}],
            "usage": {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5},
        }
    )


class _Chat:
    async def create(self, **kw: Any) -> ChatCompletion:
        if kw.get("tools"):
            if any(isinstance(m, dict) and m.get("role") == "tool" for m in kw["messages"]):
                return _completion("Uite câteva creme hidratante din catalog.")
            return _completion(tool=("search_products", {"query": "crema hidratanta", "limit": 6}))
        if isinstance(kw.get("response_format"), dict):
            return _completion("{}")
        return _completion("ok")


class _Moderation:
    async def create(self, **kw: Any) -> ModerationCreateResponse:
        from openai.types.moderation import Categories  # noqa: PLC0415

        names = [f.alias or n for n, f in Categories.model_fields.items()]
        return ModerationCreateResponse.model_validate(
            {
                "id": "m",
                "model": "omni-moderation-latest",
                "results": [
                    {
                        "flagged": False,
                        "categories": {n: False for n in names},
                        "category_scores": {n: 0.0 for n in names},
                        "category_applied_input_types": {n: ["text"] for n in names},
                    }
                ],
            }
        )


async def test_real_pipeline_turns_replay_identically(monkeypatch):
    from scripts.sim import web_audit as wa  # noqa: PLC0415
    from src.agent import llm as llm_mod  # noqa: PLC0415
    from src.config import get_settings  # noqa: PLC0415
    from src.db.connection import admin_conn, close_pool, get_pool, tenant_conn  # noqa: PLC0415
    from src.db.queries.channels import resolve_web_session  # noqa: PLC0415
    from src.evals import trace_replay as tr  # noqa: PLC0415

    s = get_settings()
    monkeypatch.setattr(s, "conversation_trace_enabled", True)
    monkeypatch.setattr(s, "trace_model_io_enabled", True)
    wa._install_fake_redis()
    fake = llm_mod.LLMClient(
        NS(
            chat=NS(completions=_Chat()), moderations=_Moderation(), responses=None, embeddings=None
        ),
        model_agent=s.model_agent,
    )
    monkeypatch.setattr(llm_mod, "_llm", fake)
    pool = await get_pool()
    try:
        async with admin_conn(pool) as conn:
            if await resolve_web_session(conn, TOKEN) is None:
                pytest.skip("canalul webchat `sole-ro` nu există pe DB-ul acesta")
        vid, sig = await wa._session(TOKEN, "nx366_replay_db")
        client = wa.WebClient(TOKEN, vid, sig, "nx366_replay_db")
        for msg in MESSAGES:
            await client.say(msg)
        async with admin_conn(pool) as conn:
            cid = await conn.fetchval(
                """select cv.id::text from channel_identities ci
                     join conversations cv
                       on cv.contact_id = ci.contact_id and cv.business_id = ci.business_id
                    where ci.business_id = $1 and ci.external_id = $2""",
                BIZ,
                vid,
            )
        async with tenant_conn(BIZ) as conn:
            rows = [
                dict(r)
                for r in await conn.fetch(
                    """select turn_id::text, business_id::text, created_at, client_text, reply,
                              diagnostics
                         from conversation_traces
                        where business_id = $1 and conversation_id = $2
                        order by created_at""",
                    BIZ,
                    cid,
                )
            ]
        assert len(rows) == len(MESSAGES)
        for row in rows:
            row["reply"] = (
                json.loads(row["reply"]) if isinstance(row["reply"], str) else row["reply"]
            )
            diag = row["diagnostics"]
            row["diagnostics"] = json.loads(diag) if isinstance(diag, str) else diag
            assert row["diagnostics"]["model_io"]["replayable"] is True
            res = await tr.replay_turn(row)
            assert res.status == "replayed", (row["client_text"], res.reason, res.divergence)
            assert res.inputs_changed == [], row["client_text"]
            assert tr.reply_fingerprint(res.reply) == tr.reply_fingerprint(row["reply"])
    finally:
        async with admin_conn(pool) as conn:
            await wa._purge_audit(conn, BIZ)
        await close_pool()
