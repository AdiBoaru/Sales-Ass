"""NX-211 production guard that wraps the existing response renderer."""

from __future__ import annotations

import json
from typing import Any

from src.agent.answer_plan import validate_answer_plan
from src.agent.answer_plan_runtime import (
    build_answer_plan_context,
    plan_event,
    prepare_answer_plan,
    run_semantic_critic,
    safe_fallback,
    validate_revised_draft,
)
from src.agent.validator import ValidationResult


async def enforce_answer_plan(
    ctx,
    deps,
    response_plan,
    legacy_render,
    *,
    products: list[dict[str, Any]],
    successful_action_ids: set[str],
    critic_enabled: bool,
    coverage_threshold: float,
    max_quality: bool,
) -> ValidationResult:
    """Plan, validate, render, criticize, revise once, then validate the served draft."""

    context = build_answer_plan_context(
        business_id=ctx.business.id,
        locale=ctx.language,
        products=products,
        successful_action_ids=successful_action_ids,
    )
    prepared = await prepare_answer_plan(
        deps.llm,
        query=response_plan.query or ctx.message.body,
        context=context,
    )
    ctx.emit("answer_plan", **plan_event(prepared))
    if prepared.plan is None:
        ctx.answer_plan = None
        ctx.set_reply(safe_fallback(ctx.language), cacheable=False)
        return ValidationResult(ok=False, reasons=["answer_plan_invalid"])

    ctx.answer_plan = prepared.plan
    render_validation: ValidationResult | None = None
    if not response_plan.handled:
        render_validation = await legacy_render(ctx, deps, response_plan)
    if ctx.reply is None or not ctx.reply.text:
        ctx.set_reply(safe_fallback(ctx.language), cacheable=False)
        return ValidationResult(ok=False, reasons=["empty_text"])

    draft_validation = validate_revised_draft(
        ctx.reply.text,
        products=products,
        generated_links=response_plan.generated_links,
        grounded_prices=response_plan.grounded_prices,
        plan=prepared.plan,
    )
    if not draft_validation.ok:
        ctx.emit("answer_plan_final_validation", ok=False, reasons=draft_validation.reasons)
        ctx.set_reply(safe_fallback(ctx.language), cacheable=False)
        return draft_validation

    critic = await run_semantic_critic(
        deps.llm,
        plan=prepared.plan,
        context=context,
        draft=ctx.reply.text,
        enabled=critic_enabled,
        comparison=bool(response_plan.compared),
        initial_validation_failed=bool(render_validation and not render_validation.ok),
        coverage_threshold=coverage_threshold,
        max_quality=max_quality,
    )
    ctx.emit(
        "answer_plan_critic",
        status=critic.status,
        triggers=list(critic.triggers),
        failures=list(critic.failures),
    )
    if critic.status == "rejected":
        revision_prompt = {
            "draft": ctx.reply.text,
            "plan": prepared.plan.model_dump(mode="json", by_alias=True),
            "critic_failures": list(critic.failures),
            "rule": "Revise once. Add no product, fact, price, stock, URL, or action.",
        }
        try:
            revised = await deps.llm.complete(
                "Revise using only the validated AnswerPlan. Return response text.",
                json.dumps(revision_prompt, ensure_ascii=False, sort_keys=True),
            )
        except Exception:  # noqa: BLE001 - revision outage must still produce a reply
            revised = ""
        final_validation = validate_revised_draft(
            revised,
            products=products,
            generated_links=response_plan.generated_links,
            grounded_prices=response_plan.grounded_prices,
            plan=prepared.plan,
        )
        if not revised or not final_validation.ok:
            ctx.emit(
                "answer_plan_final_validation",
                ok=False,
                reasons=final_validation.reasons if revised else ["empty_text"],
            )
            ctx.set_reply(safe_fallback(ctx.language), cacheable=False)
            return final_validation
        existing_products = ctx.reply.products
        ctx.set_reply(revised, products=existing_products, cacheable=False)
        draft_validation = final_validation

    final_plan_validation = validate_answer_plan(prepared.plan, context)
    if not final_plan_validation.ok:
        ctx.emit(
            "answer_plan_final_validation",
            ok=False,
            reasons=list(final_plan_validation.failures),
        )
        ctx.set_reply(safe_fallback(ctx.language), cacheable=False)
        return ValidationResult(ok=False, reasons=["answer_plan_invalid"])
    ctx.emit("answer_plan_final_validation", ok=True, reasons=[])
    return draft_validation
