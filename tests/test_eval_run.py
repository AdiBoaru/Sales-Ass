"""NX-180 — teste pt logica evaluatorului (`eval_run`) care NU cere apeluri live.

Acoperă exact fix-urile din review-ul Codex #234: judge-ul vede întrebarea curentă, redactarea
PII, hash-ul fixture-urilor independent de LF/CRLF, metrica joint natural∧answered și p95 pe raw.
Importul lui `eval_run` nu atinge DB/OpenAI (setează doar env-ul + patch-uri la runtime în main()).
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts" / "sim"))

import eval_run  # noqa: E402


def test_redact_pii():
    assert eval_run._redact("sună-mă la 0722 123 456") == "sună-mă la [REDACTED]"
    assert eval_run._redact("mail: ana.pop@example.com te rog") == "mail: [REDACTED] te rog"
    assert eval_run._redact("crema costă 89.99 lei") == "crema costă 89.99 lei"  # preț ≠ PII
    assert eval_run._redact("") == ""


def test_fixtures_signature_line_ending_independent(tmp_path, monkeypatch):
    """Fix #234: hash pe JSON canonic → LF și CRLF produc ACEEAȘI semnătură (git convertește)."""
    payload = {"conversations": [{"id": "x", "turns": [{"user": "salut", "gates": {}}]}]}
    body = json.dumps(payload, ensure_ascii=False, indent=2)

    d_lf = tmp_path / "lf"
    d_lf.mkdir()
    (d_lf / "c.json").write_bytes(body.replace("\r\n", "\n").encode("utf-8"))
    monkeypatch.setattr(eval_run, "CONV_DIR", d_lf)
    sig_lf = eval_run._fixtures_signature()

    d_crlf = tmp_path / "crlf"
    d_crlf.mkdir()
    (d_crlf / "c.json").write_bytes(body.replace("\n", "\r\n").encode("utf-8"))
    monkeypatch.setattr(eval_run, "CONV_DIR", d_crlf)
    sig_crlf = eval_run._fixtures_signature()

    assert sig_lf == sig_crlf and len(sig_lf) == 16


def test_p95_nearest_rank():
    assert eval_run._p95([]) == 0.0
    assert eval_run._p95([10.0]) == 10.0
    # p95 nearest-rank pe 20 valori = a 19-a sortată
    assert eval_run._p95([float(i) for i in range(1, 21)]) == 19.0


def _turn(natural, answered, latencies):
    """Construiește forma minimă de tur agregat pe care o consumă `_summarize`."""
    return {
        "user": "u",
        "judge": {
            "natural": {"median": natural, "spread": 0},
            "answered": {"median": answered, "spread": 0},
        },
        "gate_pass_runs": 1,
        "runs": 1,
        "gate_fails_union": [],
        "opening_repeat_runs": 0,
        "latency_ms_raw": latencies,
        "unstable": False,
    }


def test_summarize_joint_metric_and_latency_over_raw():
    cases = [
        {
            "id": "c1",
            "turns": [
                _turn(5, 5, [100.0, 200.0]),  # natural∧answered ≥4 ✓
                _turn(5, 2, [300.0]),  # natural bun, answered slab → NU joint
            ],
        },
        {"id": "c2", "turns": [_turn(2, 5, [400.0])]},  # answered bun, natural slab → NU joint
    ]
    s = eval_run._summarize(cases)
    # doar 1 din 3 tururi are AMBELE ≥4
    assert s["pct_turns_natural_AND_answered_ge4"] == round(100 / 3, 1)
    # p95 GLOBAL pe TOATE latențele brute (100,200,300,400), nu p95-de-p95-uri
    assert s["n_latency_samples"] == 4
    assert s["latency_ms_p95"] == 400.0


class _FakeTurn:
    def __init__(self, content):
        self.content = content
        self.products = []
        self.suggestions = []
        self.offer = None


class _FakeClient:
    async def say(self, msg):
        return _FakeTurn(f"răspunsul botului la: {msg}")


class _RecordingLLM:
    """Înregistrează mesajul USER pe care-l primește judge-ul (ca să dovedim că vede întrebarea)."""

    model_agent = "fake"

    def __init__(self):
        self.seen_user_msgs = []

    async def complete_schema(self, system, user, schema, model=None):
        self.seen_user_msgs.append(user)
        return {
            m: 4 for m in ("answered", "natural", "non_repetitive", "concise", "honest", "overall")
        }


async def test_judge_sees_current_question():
    """Fix BLOCANT #234: judge-ul primește transcript-ul INCLUZÂND întrebarea curentă (înainte era
    chemat înainte de a adăuga user_msg → evalua 'answered' orb la întrebare)."""
    llm = _RecordingLLM()

    async def mk(_label):
        return _FakeClient()

    convo = {"id": "t", "turns": [{"user": "am tenul gras, ce ser?", "gates": {}}]}
    await eval_run._run_conversation(convo, mk, llm, 1)

    assert llm.seen_user_msgs, "judge-ul n-a fost chemat"
    assert any("am tenul gras, ce ser?" in u for u in llm.seen_user_msgs)
