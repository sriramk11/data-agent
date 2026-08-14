"""The plan -> act -> observe -> retry loop.

This is the heart of the project. Design summary:

  - Each "turn" is one non-streaming call to the Messages API. We stream our
    own step events to the client via SSE around it, rather than using
    token-level model streaming -- simpler, and still gives a live view of
    the agent's progress.
  - Structured outputs make the loop reliable: the model's only way to act
    is one of three typed tools (run_python, make_chart, submit_answer),
    validated against Pydantic models before dispatch. We never parse prose
    to figure out what happened.
  - Failure handling, the actual point of the exercise:
      * broken generated code -> sandbox returns a traceback as a tool_result
        with is_error=True; the model sees it next turn and self-corrects.
      * infinite loops -> MAX_STEPS hard step budget.
      * runaway spend -> MAX_COST_USD budget, checked every turn.
      * repeated identical failures -> MAX_CONSECUTIVE_ERRORS short-circuits
        before the step budget is exhausted on the same mistake.
      * wandering off task -> the tool set is fixed to these three verbs, no
        shell/network/file-anywhere tool exists to wander with.
    When any budget is hit, we force a final turn with
    tool_choice={"type": "tool", "name": "submit_answer"} so the user still
    gets a structured answer (marked low-confidence) instead of a dangling
    session.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, AsyncIterator

import anthropic

from app import config
from app.agent.cost import Usage
from app.agent.prompts import FORCED_FINAL_NUDGE, STALL_NUDGE, build_system_prompt
from app.agent.tools import ARG_MODELS, TOOLS, scan_for_forbidden_patterns, validate_tool_args
from app.data_sources import schema_prompt_block
from app.sandbox.manager import SandboxCrashed, SandboxTimeout
from app.session import PendingApproval, Session

logger = logging.getLogger("orchestrator")

_client = anthropic.AsyncAnthropic(api_key=config.ANTHROPIC_API_KEY)


def _event(type_: str, **data: Any) -> dict:
    return {"type": type_, "data": data}


async def run_query(session: Session, question: str, require_approval: bool) -> AsyncIterator[dict]:
    data_path = session.sandbox.data_path(session.filename)
    schema_block = schema_prompt_block(data_path, session.columns, session.sample_rows, session.row_count)
    system_prompt = build_system_prompt(schema_block, session.sandbox.output_dir_path)

    messages = session.history
    messages.append({"role": "user", "content": question})

    step = 0
    consecutive_errors = 0
    force_final = False

    while True:
        step += 1
        budget_exhausted = step > config.MAX_STEPS or session.cost.total_usd > config.MAX_COST_USD
        if budget_exhausted or force_final:
            yield _event(
                "error",
                message=(
                    "Step budget exhausted." if step > config.MAX_STEPS
                    else "Cost budget exhausted." if budget_exhausted
                    else "Too many consecutive tool failures."
                ),
            )
            messages.append({"role": "user", "content": FORCED_FINAL_NUDGE})
            async for ev in _final_turn(session, system_prompt, messages):
                yield ev
            return

        try:
            response = await _client.messages.create(
                model=config.MODEL,
                max_tokens=config.MAX_TOKENS_PER_TURN,
                system=system_prompt,
                messages=messages,
                tools=TOOLS,
                tool_choice={"type": "auto"},
                output_config={"effort": "medium"},
            )
        except anthropic.APIError as e:
            yield _event("error", message=f"Model call failed: {e}")
            return

        usage = Usage(
            input_tokens=response.usage.input_tokens,
            output_tokens=response.usage.output_tokens,
            cache_read_input_tokens=getattr(response.usage, "cache_read_input_tokens", 0) or 0,
            cache_creation_input_tokens=getattr(response.usage, "cache_creation_input_tokens", 0) or 0,
        )
        turn_cost = session.cost.add(usage)
        yield _event(
            "cost",
            turn_usd=round(turn_cost, 5),
            cumulative_usd=round(session.cost.total_usd, 4),
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
        )

        messages.append({"role": "assistant", "content": response.content})

        text_blocks = [b.text for b in response.content if b.type == "text" and b.text.strip()]
        if text_blocks:
            yield _event("plan", text="\n".join(text_blocks))

        tool_use_blocks = [b for b in response.content if b.type == "tool_use"]

        if not tool_use_blocks:
            messages.append({"role": "user", "content": STALL_NUDGE})
            continue

        answered = False
        tool_results: list[dict] = []

        for tu in tool_use_blocks:
            yield _event("tool_call", id=tu.id, tool=tu.name, input=tu.input)

            args, err = validate_tool_args(tu.name, tu.input)
            if err:
                consecutive_errors += 1
                yield _event("tool_result", id=tu.id, tool=tu.name, ok=False, stderr=err)
                tool_results.append(_error_result(tu.id, err))
                continue

            if tu.name == "submit_answer":
                yield _event(
                    "answer",
                    answer=args.answer,
                    key_findings=args.key_findings,
                    chart_files=args.chart_files,
                    confidence=args.confidence,
                    caveats=args.caveats,
                    chart_urls=[f"/api/sessions/{session.session_id}/charts/{f}" for f in args.chart_files],
                )
                answered = True
                break

            # run_python / make_chart: defense-in-depth static scan before
            # spending a sandbox exec on obviously adversarial code.
            violations = scan_for_forbidden_patterns(args.code)
            if violations:
                consecutive_errors += 1
                msg = "Blocked before execution: " + "; ".join(violations)
                yield _event("tool_result", id=tu.id, tool=tu.name, ok=False, stderr=msg)
                tool_results.append(_error_result(tu.id, msg))
                continue

            code_to_run = args.code
            if require_approval:
                approval = PendingApproval(tool_id=tu.id, tool_name=tu.name, code=code_to_run)
                session.pending_approval = approval
                yield _event("pending_approval", id=tu.id, tool=tu.name, code=code_to_run)
                await approval.ready.wait()
                session.pending_approval = None
                if not approval.approved:
                    msg = "User rejected this step before it ran."
                    yield _event("tool_result", id=tu.id, tool=tu.name, ok=False, stderr=msg)
                    tool_results.append(_error_result(tu.id, msg))
                    continue
                if approval.edited_code:
                    code_to_run = approval.edited_code

            ok, stdout, stderr, elapsed_ms, chart_urls = await _run_in_sandbox(session, code_to_run)
            consecutive_errors = 0 if ok else consecutive_errors + 1
            yield _event(
                "tool_result", id=tu.id, tool=tu.name, ok=ok,
                stdout=stdout[-4000:], stderr=stderr[-4000:],
                elapsed_ms=elapsed_ms, chart_urls=chart_urls,
            )
            content = f"stdout:\n{stdout}\n\nstderr:\n{stderr}" if stderr else f"stdout:\n{stdout}"
            if chart_urls:
                content += f"\n\nchart files saved: {chart_urls}"
            tool_results.append({
                "type": "tool_result", "tool_use_id": tu.id,
                "content": content[-6000:], "is_error": not ok,
            })

        if answered:
            yield _event("done")
            return

        if consecutive_errors >= config.MAX_CONSECUTIVE_ERRORS:
            force_final = True

        if tool_results:
            messages.append({"role": "user", "content": tool_results})


async def _final_turn(session: Session, system_prompt: str, messages: list[dict]) -> AsyncIterator[dict]:
    """Force a submit_answer call when a budget is exhausted, so the user
    gets a structured (if low-confidence) result instead of nothing."""
    try:
        response = await _client.messages.create(
            model=config.MODEL,
            max_tokens=config.MAX_TOKENS_PER_TURN,
            system=system_prompt,
            messages=messages,
            tools=TOOLS,
            tool_choice={"type": "tool", "name": "submit_answer"},
        )
    except anthropic.APIError as e:
        yield _event("error", message=f"Final synthesis call failed: {e}")
        yield _event("done")
        return

    usage = Usage(
        input_tokens=response.usage.input_tokens, output_tokens=response.usage.output_tokens,
    )
    session.cost.add(usage)

    for b in response.content:
        if b.type == "tool_use" and b.name == "submit_answer":
            args, err = validate_tool_args("submit_answer", b.input)
            if err:
                yield _event("error", message=f"Malformed final answer: {err}")
                break
            yield _event(
                "answer",
                answer=args.answer, key_findings=args.key_findings,
                chart_files=args.chart_files, confidence="low",
                caveats=args.caveats or "Stopped early due to a budget limit.",
                chart_urls=[f"/api/sessions/{session.session_id}/charts/{f}" for f in args.chart_files],
            )
            break
    yield _event("done")


async def _run_in_sandbox(session: Session, code: str) -> tuple[bool, str, str, int, list[str]]:
    try:
        result = await _to_thread(session.sandbox.exec, code, config.SANDBOX_EXEC_TIMEOUT_S)
    except SandboxTimeout:
        return False, "", f"Execution timed out after {config.SANDBOX_EXEC_TIMEOUT_S}s. Sandbox restarted.", 0, []
    except SandboxCrashed as e:
        return False, "", f"Sandbox crashed: {e}. It will be restarted on the next call.", 0, []

    new_files = result.get("new_files", [])
    chart_urls = [f"/api/sessions/{session.session_id}/charts/{f}" for f in new_files]
    return result["ok"], result.get("stdout", ""), result.get("stderr", ""), result.get("elapsed_ms", 0), chart_urls


async def _to_thread(fn, *args):
    return await asyncio.get_event_loop().run_in_executor(None, fn, *args)


def _error_result(tool_use_id: str, message: str) -> dict:
    return {"type": "tool_result", "tool_use_id": tool_use_id, "content": message, "is_error": True}
