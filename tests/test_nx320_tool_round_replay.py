"""NX-320 felia 1 — agregarea și verdictul replay-ului (PURE, fără DB, fără model).

Ce se pinuiește: regula de decizie PRE-ÎNREGISTRATĂ (`GO_RULE`) și cele patru ieșiri ale ei.
`INSUFFICIENT` e distinct de `NO-GO` (ca la NX-238/246): „n-am avut ce măsura" nu e „am măsurat și
a picat". Și că mutațiile cerute de model se NUMĂRĂ, dar nu se execută niciodată.
"""

from scripts.nx320_tool_round_replay import (
    BASELINE,
    CONTROL,
    GO_RULE,
    arms_for,
    blind_pairs,
    summarize,
    verdict,
)

CAND = "responses:low"
ARMS = arms_for(("low",))


def _search(*, contradicted=False, unsourced=False, category=None, served=6):
    return {
        "category": category,
        "category_guessed": bool(category),
        "facets_guessed": False,
        "contradicted": contradicted,
        "price_unsourced": unsourced,
        "off_menu": False,
        "lexical_step": "strict",
        "served": served,
        "top": ["X"],
    }


def _arm(ms, evals, *, ok=True, calls=None):
    return {
        "ok": ok,
        "ms": ms,
        "cost_usd": 0.0001,
        "reasoning_tokens": 0,
        "error": None if ok else "BadRequestError",
        "calls": calls if calls is not None else [{"name": "search_products", "args": {}}],
        "text": "",
        "evals": evals,
    }


def _entries(n, *, base_err, cand_err, base_ms=2000, cand_ms=4000):
    out = []
    for i in range(n):
        out.append(
            {
                "turn_id": f"{i:08d}-aaaa",
                "order": list(ARMS),
                "results": {
                    BASELINE: _arm(base_ms, [_search(contradicted=i < base_err)]),
                    CONTROL: _arm(base_ms, [_search(contradicted=i < base_err)]),
                    CAND: _arm(cand_ms, [_search(contradicted=i < cand_err)]),
                },
            }
        )
    return out


def test_arms_au_baseline_si_control():
    assert ARMS == ("chat:none", "responses:none", "responses:low")


def test_go_cand_greselile_scad_la_jumatate_si_latenta_incape():
    s = summarize(_entries(10, base_err=6, cand_err=2), ARMS)
    v = verdict(s, CAND)
    assert v["verdict"] == "GO"
    assert v["baseline_errors"] == 6 and v["candidate_errors"] == 2


def test_no_go_cand_reducerea_nu_ajunge():
    v = verdict(summarize(_entries(10, base_err=6, cand_err=4), ARMS), CAND)
    assert v["verdict"] == "NO-GO"


def test_no_go_cand_latenta_creste_peste_prag():
    slow = 2000 + GO_RULE["max_p50_increase_ms"] + 1
    v = verdict(summarize(_entries(10, base_err=6, cand_err=0, cand_ms=slow), ARMS), CAND)
    assert v["verdict"] == "NO-GO"


def test_insufficient_nu_e_no_go():
    v = verdict(summarize(_entries(10, base_err=2, cand_err=0), ARMS), CAND)
    assert v["verdict"] == "INSUFFICIENT"


def test_un_apel_esuat_pe_candidat_blocheaza_go():
    entries = _entries(10, base_err=6, cand_err=0)
    entries[0]["results"][CAND] = _arm(0, [], ok=False, calls=[])
    v = verdict(summarize(entries, ARMS), CAND)
    assert v["verdict"] == "NO-GO" and v["candidate_failed_rounds"] == 1


def test_pretul_fara_sursa_intra_in_greseli():
    entries = _entries(10, base_err=0, cand_err=0)
    for e in entries[:5]:
        e["results"][BASELINE]["evals"] = [_search(unsourced=True)]
    s = summarize(entries, ARMS)
    assert s[BASELINE]["price_unsourced"] == 5
    assert verdict(s, CAND)["verdict"] == "GO"


def test_mutatiile_se_numara_nu_se_executa():
    entries = _entries(4, base_err=0, cand_err=0)
    entries[0]["results"][CAND]["calls"] = [{"name": "cart_add", "args": {}}]
    entries[0]["results"][CAND]["evals"] = []
    s = summarize(entries, ARMS)
    assert s[CAND]["mutations_requested"] == 1


def test_perechile_sunt_oarbe_si_deterministe():
    entries = _entries(6, base_err=2, cand_err=1)
    p1, k1 = blind_pairs(entries, CAND, seed=7)
    p2, k2 = blind_pairs(entries, CAND, seed=7)
    assert (p1, k1) == (p2, k2)
    assert all(set(k.values()) == {BASELINE, CAND} for k in k1.values())
    assert all("chat:none" not in p["A"] + p["B"] for p in p1)  # brațul nu apare în text
