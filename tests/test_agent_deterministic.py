"""NX-143 — teste de gating pentru intențiile deterministe PRE-loop (`src/agent/deterministic.py`).

Comportamentul handler-elor (link/compare) e acoperit end-to-end de `test_link_intent.py` /
`test_agent.py`; aici testăm predicatele de gating izolat (True/False), pe un `ctx` minimal.
"""

from types import SimpleNamespace

from src.agent import deterministic as det
from src.models import Route


def _ctx(body, *, route=Route.SALES, filters=None, active_search=None, displayed=None, pack=None):
    ctx = SimpleNamespace(
        route=SimpleNamespace(route=route, filters=filters),
        message=SimpleNamespace(body=body),
        state=SimpleNamespace(active_search=active_search, displayed_products=displayed or []),
        # Gardul de rafinare citește mesajul BRUT prin extractorul NX-208 → are nevoie de pack +
        # limbă. `pack=None` = degradare grațioasă (doar tiparele de limbă), exact ca `sole-ro`.
        business=SimpleNamespace(domain_pack=pack),
        language="ro",
        events=[],
    )
    ctx.emit = lambda name, **props: ctx.events.append((name, props))
    return ctx


def _settings(monkeypatch, **flags):
    base = dict(
        search_sessions_enabled=True,
        link_intent_enabled=True,
        compare_intent_enabled=True,
        refinement_guard_enabled=True,
    )
    base.update(flags)
    monkeypatch.setattr(det, "get_settings", lambda: SimpleNamespace(**base))


# --- is_show_more ----------------------------------------------------------- #


def test_show_more_true_on_active_session(monkeypatch):
    _settings(monkeypatch)
    assert det.is_show_more(_ctx("mai arată-mi", active_search={"fp": "x"})) is True


def test_show_more_false_without_session(monkeypatch):
    _settings(monkeypatch)
    assert det.is_show_more(_ctx("mai arată-mi", active_search=None)) is False


def test_show_more_false_with_new_filters(monkeypatch):
    # constrângere nouă = RAFINARE, nu paginare → cade pe bucla LLM
    _settings(monkeypatch)
    ctx = _ctx("mai multe sub 50", active_search={"fp": "x"}, filters={"budget_max": 50})
    assert det.is_show_more(ctx) is False


def test_show_more_false_on_cheaper(monkeypatch):
    # „mai ieftin" = cheaper_intent (post-loop), nu paginare
    _settings(monkeypatch)
    assert det.is_show_more(_ctx("ceva mai ieftin", active_search={"fp": "x"})) is False


def test_show_more_false_when_disabled(monkeypatch):
    _settings(monkeypatch, search_sessions_enabled=False)
    assert det.is_show_more(_ctx("mai arată-mi", active_search={"fp": "x"})) is False


def test_show_more_false_on_order_route(monkeypatch):
    _settings(monkeypatch)
    ctx = _ctx("mai arată-mi", route=Route.ORDER, active_search={"fp": "x"})
    assert det.is_show_more(ctx) is False


# --- try_pre_intents (guard-uri) -------------------------------------------- #


async def test_pre_intents_false_on_order(monkeypatch):
    _settings(monkeypatch)
    assert await det.try_pre_intents(_ctx("dă-mi linkul", route=Route.ORDER), object()) is False


async def test_pre_intents_false_on_empty_query(monkeypatch):
    _settings(monkeypatch)
    assert await det.try_pre_intents(_ctx("   "), object()) is False


async def test_pre_intents_false_link_with_new_filters(monkeypatch):
    # „link la ceva sub 50" = căutare nouă (filtru) → NU intenție de link → False (lasă bucla)
    _settings(monkeypatch)
    ctx = _ctx(
        "dă-mi linkul la o cremă sub 50", displayed=[SimpleNamespace()], filters={"budget_max": 50}
    )
    assert await det.try_pre_intents(ctx, object()) is False


async def test_pre_intents_link_calls_handler(monkeypatch):
    # gating True → handlerul de link rulează (îl stub-uim să confirme calea)
    _settings(monkeypatch)
    called = {}

    async def fake_handle(ctx, deps):
        called["link"] = True

    monkeypatch.setattr(det, "_handle_link_intent", fake_handle)
    ctx = _ctx("dă-mi linkul direct", displayed=[SimpleNamespace()])
    assert await det.try_pre_intents(ctx, object()) is True
    assert called.get("link") is True


# --- gardul de rafinare fără triaj (NX-251) --------------------------------- #
#
# Cu triajul scos de pe drumul sincron, `route.filters` e GOL la fiecare tur, deci gardul care
# despărțea scurtătura de rafinare („mai arată-mi" vs „mai arată-mi, dar sub 100 lei") era
# permanent deschis. Testele de aici țin al doilea producător — extractorul determinist pe mesajul
# BRUT — și, la fel de important, țin și cazul în care scurtătura TREBUIE să rămână ieftină.


def test_show_more_false_when_message_adds_budget_without_triage(monkeypatch):
    # REGRESIA: fără `filters` de la triaj, „mai arată-mi ceva sub 100 lei" pagina pool-ul VECHI,
    # construit fără plafon — răspuns cu produse peste buget, care trecea și de validator (prețuri
    # reale) și de grounding guard. Și, fiindcă ramura de paginare e ÎNAINTEA creierului unic,
    # mesajul nu ajungea deloc la model.
    _settings(monkeypatch)
    ctx = _ctx("mai arată-mi ceva sub 100 lei", active_search={"fp": "x"}, filters=None)
    assert det.is_show_more(ctx) is False


def test_show_more_stays_true_on_pure_pagination(monkeypatch):
    # Cealaltă jumătate a contractului: fără constrângere nouă, paginarea rămâne deterministă și
    # gratuită. Un garda prea lacom ar muta tot traficul de follow-up pe bucla LLM.
    _settings(monkeypatch)
    for phrase in ("mai arată-mi", "mai multe", "arată-mi și alte opțiuni"):
        ctx = _ctx(phrase, active_search={"fp": "x"}, filters=None)
        assert det.is_show_more(ctx) is True, phrase


def test_show_more_kill_switch_restores_old_behaviour(monkeypatch):
    _settings(monkeypatch, refinement_guard_enabled=False)
    ctx = _ctx("mai arată-mi ceva sub 100 lei", active_search={"fp": "x"}, filters=None)
    assert det.is_show_more(ctx) is True


def test_carries_new_constraints_catches_anything_outside_the_formula(monkeypatch):
    # Miezul designului: NU enumerăm constrângerile (mulțime deschisă — clienții nu scriu la fel),
    # ci scădem formula scurtăturii. Preț, brand, concern și o cerere descrisă în cuvintele
    # clientului lasă toate reziduu, deși niciuna nu e prevăzută nicăieri.
    _settings(monkeypatch)
    for phrase in (
        "mai arată-mi ceva sub 100 lei",
        "mai arată-mi, dar de la Cerave",
        "mai arată-mi, dar pentru ten gras",
        "mai arată-mi ceva ce nu mă lucește",
    ):
        assert det.carries_new_constraints(_ctx(phrase), det._MORE_RE) is True, phrase


def test_carries_new_constraints_false_on_bare_formula(monkeypatch):
    _settings(monkeypatch)
    for phrase, trigger in (
        ("mai arată-mi", det._MORE_RE),
        ("mai arată-mi te rog", det._MORE_RE),
        ("arată-mi și alte opțiuni", det._MORE_RE),
        ("dă-mi linkul direct", det._LINK_RE),
        ("compară primele două", det._COMPARE_RE),
    ):
        assert det.carries_new_constraints(_ctx(phrase), trigger) is False, phrase


def test_residue_is_computed_against_its_own_trigger(monkeypatch):
    # „linkul" e formulă pentru poarta de link și conținut pentru cea de comparație — de aceea
    # reziduul se calculează per declanșator, nu o dată pe tur.
    _settings(monkeypatch)
    assert det._shortcut_residue("dă-mi linkul", det._LINK_RE, "ro") == []
    assert det._shortcut_residue("dă-mi linkul", det._COMPARE_RE, "ro") == ["linkul"]


def test_unknown_locale_degrades_toward_the_model_not_toward_blindness(monkeypatch):
    # P11: fără tabel de limbă nu aplicăm româna peste altă limbă. Declanșatoarele sunt RO/EN/HU,
    # deci o scurtătură GOALĂ rămâne deterministă în orice limbă; doar textul din jur, pe care nu-l
    # putem citi, împinge turul la model. Degradarea costă inferențe, nu adevăr.
    _settings(monkeypatch)
    bare = _ctx("tobbet")
    bare.language = "hu"
    assert det.carries_new_constraints(bare, det._MORE_RE) is False

    refined = _ctx("tobbet, de 100 lei alatt")
    refined.language = "hu"
    assert det.carries_new_constraints(refined, det._MORE_RE) is True


def test_carries_new_constraints_ignores_empty_body(monkeypatch):
    # NX-236: un buton are `body` gol prin construcție — comanda e declarată, nu dedusă din text.
    _settings(monkeypatch)
    assert det.carries_new_constraints(_ctx(""), det._MORE_RE) is False


def test_carries_new_constraints_trusts_triage_filters_when_present(monkeypatch):
    # Calea de dinainte de NX-251 rămâne intactă: dacă triajul a extras sloturi, ele decid, iar
    # kill-switch-ul nu le poate anula (altfel oprirea gardului ar strica și comportamentul vechi).
    _settings(monkeypatch, refinement_guard_enabled=False)
    ctx = _ctx("mai arată-mi", filters={"budget_max": 50})
    assert det.carries_new_constraints(ctx, det._MORE_RE) is True


async def test_anchored_gates_keep_serving_references(monkeypatch):
    """Gardul de rafinare NU se aplică porților ancorate — pinuiește decizia, nu doar codul.

    Pe link/compare, cuvintele în plus sunt de obicei o REFERINȚĂ („linkul către crema asta"), iar
    `resolve_reference` e cel care le interpretează. Dacă cineva extinde gardul aici, testul ăsta
    pică și îl obligă să citească motivul: reziduul lexical nu deosebește referința de rafinare, iar
    căderea pe model reînvie NX-131. Prețul asumat e scris în `carries_new_constraints`."""
    _settings(monkeypatch)
    called = {}

    async def fake_handle(ctx, deps):
        called["link"] = True

    monkeypatch.setattr(det, "_handle_link_intent", fake_handle)
    ctx = _ctx("îmi dai linkul direct către crema asta?", displayed=[SimpleNamespace()])
    assert await det.try_pre_intents(ctx, object()) is True
    assert called.get("link") is True
