"""Poarta mecanică `scripts/check_no_raw_conn.py` — testată pe cod SINTETIC.

Un guard netestat e o iluzie de siguranță: dacă regex-ul/AST-ul lui se strică tăcut, CI rămâne
verde în timp ce invariantul se erodează. Aici îi dăm exemple fabricate — unele curate, altele
exact bug-ul pe care trebuie să-l prindă — și verificăm verdictul, plus faptul că `src/` real
trece azi fără nicio excepție.
"""

from __future__ import annotations

import ast
import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
_SPEC = importlib.util.spec_from_file_location(
    "check_no_raw_conn", ROOT / "scripts" / "check_no_raw_conn.py"
)
guard = importlib.util.module_from_spec(_SPEC)
sys.modules["check_no_raw_conn"] = guard
_SPEC.loader.exec_module(guard)


def _findings(code: str, rel: str = "src/worker/stages/fake.py") -> list[tuple[str, str]]:
    """Rulează regulile pe un fragment de cod, fără să atingă discul."""
    tree = ast.parse(code)
    visitor = guard._Visitor(rel)
    visitor.visit(tree)
    out = [(f.rule, f.detail) for f in visitor.findings]
    out += [(f.rule, f.detail) for f in guard._check_r3(tree, rel)]
    return out


# --------------------------------------------------------------------------- #
# R1 — conexiunea vie lungă
# --------------------------------------------------------------------------- #


def test_r1_catches_deps_conn():
    code = "async def s(ctx, deps):\n    await q(deps.conn, ctx.business.id)\n"
    assert ("R1", "deps.conn") in _findings(code)


def test_r1_catches_pipeline_deps_conn_kwarg():
    code = "def f():\n    return PipelineDeps(conn=c, llm=None)\n"
    assert ("R1", "PipelineDeps(conn=...)") in _findings(code)


def test_r1_ignores_mentions_in_comments_and_docstrings():
    # Detecție prin AST, nu prin grep: documentația care EXPLICĂ regula nu o încalcă.
    code = '"""Nu folosi deps.conn — vezi PipelineDeps(conn=...)."""\n# deps.conn e interzis\n'
    assert _findings(code) == []


def test_r1_ignores_other_conn_attributes():
    code = "async def s(ctx, deps):\n    x = self.conn\n    y = row.conn\n"
    assert _findings(code) == []


# --------------------------------------------------------------------------- #
# R2 — conexiune ținută peste un await extern (miezul cardului)
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "external",
    [
        "await deps.llm.embed([q])",
        "await deps.llm.run_tool_loop(s, u, t, e)",
        "await deps.llm.classify_json(s, u)",
        "await asyncio.sleep(0.5)",
        "await http.post(url, json=body)",
        "await sender.send_text(a, b, c)",
    ],
)
def test_r2_catches_external_await_inside_checkout(external):
    code = f"async def s(ctx, deps):\n    async with deps.db('op') as conn:\n        {external}\n"
    rules = [r for r, _ in _findings(code)]
    assert "R2" in rules, f"{external} ar fi trebuit raportat"


def test_r2_catches_it_inside_tenant_conn_too():
    code = (
        "async def s(biz):\n"
        "    async with tenant_conn(biz) as conn:\n"
        "        await llm.embed(['x'])\n"
    )
    assert [r for r, _ in _findings(code, "src/web/app.py")] == ["R2"]


def test_r2_catches_nested_external_await():
    # Ascuns într-un `if`/`try` e tot înăuntru: căutarea merge în tot corpul, nu doar la nivelul 1.
    code = (
        "async def s(ctx, deps):\n"
        "    async with deps.db('op') as conn:\n"
        "        if x:\n"
        "            try:\n"
        "                await deps.llm.embed(['q'])\n"
        "            except Exception:\n"
        "                pass\n"
    )
    assert [r for r, _ in _findings(code)] == ["R2"]


def test_r2_allows_db_calls_inside_checkout():
    code = (
        "async def s(ctx, deps):\n"
        "    async with deps.db('op') as conn:\n"
        "        rows = await conn.fetch('select 1')\n"
        "        await touch_hit(conn, ctx.business.id, rows)\n"
    )
    assert _findings(code) == []


def test_r2_allows_external_await_outside_the_checkout():
    # Tiparul CORECT: read scurt → extern → write scurt.
    code = (
        "async def s(ctx, deps):\n"
        "    async with deps.db('read') as conn:\n"
        "        v = await conn.fetchval('select 1')\n"
        "    emb = await deps.llm.embed(['q'])\n"
        "    async with deps.db('write') as conn:\n"
        "        await conn.execute('insert 1')\n"
    )
    assert _findings(code) == []


def test_r2_does_not_flag_local_get_on_a_non_http_receiver():
    # `cache.get(conn, token)` e un lookup de DB, nu o cerere de rețea — un guard care îl raportează
    # devine zgomot, iar zgomotul se dezactivează. Verbele HTTP contează doar pe un client HTTP.
    code = (
        "async def s(token):\n"
        "    async with admin_conn(pool) as conn:\n"
        "        return await get_session_cache().get(conn, token)\n"
    )
    assert _findings(code, "src/web/app.py") == []


# --------------------------------------------------------------------------- #
# R3 — punct de intrare care primește o conexiune
# --------------------------------------------------------------------------- #


def test_r3_catches_a_stage_taking_conn():
    code = "async def gates_stage(ctx, deps, conn):\n    return None\n"
    assert ("R3", "gates_stage(conn=...)") in _findings(code)


def test_r3_catches_a_registered_tool_taking_conn():
    code = '@register("search_products")\nasync def t(ctx, deps, conn):\n    return None\n'
    assert ("R3", "t(conn=...)") in _findings(code, "src/tools/x.py")


def test_r3_allows_private_helpers_taking_conn():
    # Tiparul corect pentru „mai multe query-uri atomice": un helper chemat ÎNĂUNTRUL unui
    # checkout primește `conn` — proprietatea rămâne la cel care a deschis checkout-ul.
    code = "async def _serve(ctx, conn, entry):\n    return None\n"
    assert _findings(code) == []


def test_r3_allows_repository_functions():
    code = "async def semantic_lookup(conn, business_id, locale, emb):\n    return None\n"
    assert _findings(code, "src/db/queries/faqs.py") == []


# --------------------------------------------------------------------------- #
# Allowlist + starea reală a repo-ului
# --------------------------------------------------------------------------- #


def test_allowlist_entries_require_a_reason(tmp_path, monkeypatch):
    bad = tmp_path / "allow.json"
    bad.write_text(
        '{"allow": [{"rule": "R2", "path": "src/x.py", "detail": "d"}]}', encoding="utf-8"
    )
    monkeypatch.setattr(guard, "ALLOWLIST", bad)
    with pytest.raises(SystemExit):
        guard._load_allowlist()


def test_allowlist_is_empty_at_nx231_close():
    # DoD: „zero conexiuni ținute peste LLM/HTTP în cod runtime NEALLOWLISTAT". Dacă lista începe
    # să crească, invariantul se erodează — testul ăsta o face vizibilă, nu o interzice.
    assert guard._load_allowlist() == {}


def test_real_src_tree_has_zero_violations():
    findings = guard.scan()
    allow = guard._load_allowlist()
    violations = [f for f in findings if f.key not in allow]
    detail = "\n".join(f"{f.rule} {f.path}:{f.lineno} {f.detail}" for f in violations)
    assert violations == [], detail
