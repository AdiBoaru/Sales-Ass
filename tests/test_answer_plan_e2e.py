from copy import deepcopy

from src.agent.answer_plan_guard import enforce_answer_plan
from src.agent.planner import ResponsePlan
from src.agent.validator import ValidationResult
from src.models import BusinessConfig, Contact, InboundMessage, TurnContext
from src.worker.runner import PipelineDeps

PRODUCT = {
    "id": "product-1",
    "name": "Ser Controlat",
    "price": 49.0,
    "availability": "in_stock",
    "url": "https://shop.test/product-1",
}


def _plan_json(*, valid=True, recommendation=False):
    price = 49.0 if valid else 999.0
    return {
        "schema_version": 1,
        "business_id": "business-1",
        "locale": "ro",
        "selected_products": [
            {
                "product_id": "product-1",
                "variant_id": None,
                "evidence_ids": ["product:product-1:identity"],
            }
        ],
        "claims": [
            {
                "type": "recommendation" if recommendation else "fact",
                "text": "Produsul este disponibil.",
                "evidence_ids": ["product:product-1:stock"],
                "need_ids": [],
            }
        ],
        "facts": {
            "prices": [
                {
                    "product_id": "product-1",
                    "variant_id": None,
                    "value": price,
                    "evidence_id": "product:product-1:price",
                }
            ],
            "stocks": [
                {
                    "product_id": "product-1",
                    "variant_id": None,
                    "value": "in_stock",
                    "evidence_id": "product:product-1:stock",
                }
            ],
            "urls": [
                {
                    "product_id": "product-1",
                    "variant_id": None,
                    "value": "https://shop.test/product-1",
                    "evidence_id": "product:product-1:url",
                }
            ],
        },
        "uncertainties": [],
        "unmet_constraints": [],
        "confirmed_actions": [],
    }


class _LLM:
    def __init__(self, schema_outputs, *, revision="Varianta revizuita este disponibila."):
        self.schema_outputs = list(schema_outputs)
        self.revision = revision
        self.schema_calls = 0
        self.complete_calls = 0

    async def complete_schema(self, system, user, schema, *, model=None):
        output = self.schema_outputs[self.schema_calls]
        self.schema_calls += 1
        if isinstance(output, Exception):
            raise output
        return deepcopy(output)

    async def complete(self, system, user, *, model=None):
        self.complete_calls += 1
        return self.revision


def _ctx():
    return TurnContext(
        turn_id="turn-1",
        business=BusinessConfig(id="business-1", slug="demo", name="Demo"),
        contact=Contact(id="contact-1", business_id="business-1"),
        message=InboundMessage(provider_msg_id="message-1", body="vreau un ser"),
        conversation_id="conversation-1",
    )


def _response_plan():
    return ResponsePlan(
        products=[dict(PRODUCT)],
        query="vreau un ser",
        generated_links=set(),
        grounded_prices=set(),
    )


async def _legacy_render(ctx, deps, plan):
    ctx.set_reply("Serul este disponibil la 49 lei.")
    return ValidationResult(ok=True, reasons=[])


async def test_enabled_flow_emits_validated_plan_before_serving_draft():
    llm = _LLM([_plan_json()])
    ctx = _ctx()

    result = await enforce_answer_plan(
        ctx,
        PipelineDeps(conn=object(), redis=None, llm=llm),
        _response_plan(),
        _legacy_render,
        products=[dict(PRODUCT)],
        successful_action_ids=set(),
        critic_enabled=False,
        coverage_threshold=0.99,
        max_quality=False,
    )

    assert result.ok is True
    assert ctx.answer_plan is not None and ctx.answer_plan.schema_version == 1
    assert ctx.reply is not None and "49" in ctx.reply.text
    event = next(event for event in ctx.events if event.type == "answer_plan")
    assert "claims" not in event.properties
    assert event.properties["ok"] is True


async def test_two_invalid_plans_produce_visible_safe_fallback():
    llm = _LLM([_plan_json(valid=False), _plan_json(valid=False)])
    ctx = _ctx()

    result = await enforce_answer_plan(
        ctx,
        PipelineDeps(conn=object(), redis=None, llm=llm),
        _response_plan(),
        _legacy_render,
        products=[dict(PRODUCT)],
        successful_action_ids=set(),
        critic_enabled=False,
        coverage_threshold=0.99,
        max_quality=False,
    )

    assert result.ok is False
    assert llm.schema_calls == 2
    assert ctx.reply is not None and ctx.reply.text
    assert "999" not in ctx.reply.text
    assert ctx.reply.cacheable is False


async def test_critic_unavailable_keeps_deterministically_validated_draft():
    llm = _LLM([_plan_json(recommendation=True), RuntimeError("critic down")])
    ctx = _ctx()

    result = await enforce_answer_plan(
        ctx,
        PipelineDeps(conn=object(), redis=None, llm=llm),
        _response_plan(),
        _legacy_render,
        products=[dict(PRODUCT)],
        successful_action_ids=set(),
        critic_enabled=True,
        coverage_threshold=0.99,
        max_quality=False,
    )

    assert result.ok is True
    assert ctx.reply is not None and "49" in ctx.reply.text
    critic = next(event for event in ctx.events if event.type == "answer_plan_critic")
    assert critic.properties["status"] == "unavailable"


async def test_rejected_critic_and_invalid_revision_fall_back_without_silence():
    llm = _LLM(
        [
            _plan_json(recommendation=True),
            {"passed": False, "failures": ["claim_not_supported"]},
        ],
        revision="Costa 999 lei: https://fake.test/pay",
    )
    ctx = _ctx()

    result = await enforce_answer_plan(
        ctx,
        PipelineDeps(conn=object(), redis=None, llm=llm),
        _response_plan(),
        _legacy_render,
        products=[dict(PRODUCT)],
        successful_action_ids=set(),
        critic_enabled=True,
        coverage_threshold=0.99,
        max_quality=False,
    )

    assert result.ok is False
    assert llm.complete_calls == 1
    assert ctx.reply is not None and ctx.reply.text
    assert "999" not in ctx.reply.text
    assert "fake.test" not in ctx.reply.text
