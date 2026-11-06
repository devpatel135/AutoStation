"""AUTOSTATION Studio design chain.

Interactive, step-by-step automation design grounded in the AUTOSTATION context frame.
Each step is a narrow LLM call that accumulates into a DesignSession. Steps can be
revisited (re-run with clarifications added) before moving forward. The final step
produces a plain-language design contract the user approves before code generation.

Design chain (in order):
  1. GOAL      — clarify goal, suggest name, identify target system + classification
  2. ITEMS     — define the atomic work unit (item model for log_item())
  3. ERRORS    — map failure surface to NETWORK / DATA_ERROR / LOGIC_ERROR
  4. LLM       — determine LLM integration point (patterns 1-4 or none)
  5. SCHEDULE  — cron, timeout, synopsis routing, output channel
  → CONTRACT   — plain-language design contract + project.yaml + developer task list

Usage (non-interactive / batch):
    ctx = build_studio_context()
    session = DesignSession.start("Renew expiring certs from the CA portal", ctx)
    run_design_chain(session)   # runs all 5 steps + contract
    contract = session.result(StepName.CONTRACT)

Usage (interactive, one step at a time):
    ctx = build_studio_context()
    session = DesignSession.start(goal, ctx)
    goal_result = run_step(session, StepName.GOAL)
    # inspect open_questions, add answers, then continue
    session.clarifications.append("The cert portal URL is https://ca.internal/portal")
    items_result = run_step(session, StepName.ITEMS)
    ...
    contract = run_step(session, StepName.CONTRACT)
"""
from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from .context import StudioContext


# ── Step names ────────────────────────────────────────────────────────────────

class StepName(str, Enum):
    GOAL     = "goal_refinement"
    ITEMS    = "item_model"
    ERRORS   = "error_classes"
    LLM      = "llm_integration"
    SCHEDULE = "schedule_output"
    CONTRACT = "contract"

DESIGN_STEPS = (StepName.GOAL, StepName.ITEMS, StepName.ERRORS, StepName.LLM, StepName.SCHEDULE)


# ── Output schemas (passed to llm_call schema= for validated JSON) ────────────

GOAL_SCHEMA = {
    "automation_name":     "string",   # kebab: automation-<noun>-<verb>
    "purpose":             "string",   # ≤ 2 sentences
    "target_system":       "string",   # app / service being automated
    "data_classification": "public|internal|confidential|restricted",
    "open_questions":      ["string"], # keep short; empty if goal is clear
}

ITEM_SCHEMA = {
    "item_noun":               "string",  # e.g. "certificate"
    "item_id_field":           "string",  # e.g. "cert_id"
    "item_id_example":         "string",  # e.g. "CERT-2024-001"
    "outcome_categories":      ["string"],# ["success", "skipped", "failed"]
    "skip_reasons":            ["string"],
    "failure_modes":           ["string"],
    "estimated_items_per_run": "string",  # rough range, e.g. "1–20"
}

ERROR_SCHEMA = {
    "network_scenarios":      ["string"],
    "data_error_scenarios":   ["string"],
    "logic_error_scenarios":  ["string"],
    "retry_strategy":         "string",  # plain English
    "max_retries":            "integer",
}

LLM_SCHEMA = {
    "needs_llm":          "boolean",
    "pattern":            "none|pattern1|pattern2|pattern3|pattern4",
    "pattern_description":"string",  # what the LLM does at runtime (empty if none)
    "caller_tag":         "string",  # e.g. "cert-renewal/classify"
}

SCHEDULE_SCHEMA = {
    "cron":               "string",  # e.g. "0 6 * * 1"
    "cron_human":         "string",  # e.g. "Every Monday at 6 AM"
    "timeout_s":          "integer",
    "synopsis_target":    "string",  # "teams" | "email" | "log"
    "output_description": "string",  # one sentence: what the user sees on completion
}

CONTRACT_SCHEMA = {
    "plain_language":           "string",  # 3-5 paragraphs, jargon-free
    "developer_tasks":          ["string"],# what still needs manual implementation
    "project_yaml":             "object",  # ready-to-write project.yaml dict
    "automation_yaml_additions":"object",  # extra automation.yaml fields beyond defaults
}

STEP_SCHEMAS: dict[StepName, Any] = {
    StepName.GOAL:     GOAL_SCHEMA,
    StepName.ITEMS:    ITEM_SCHEMA,
    StepName.ERRORS:   ERROR_SCHEMA,
    StepName.LLM:      LLM_SCHEMA,
    StepName.SCHEDULE: SCHEDULE_SCHEMA,
    StepName.CONTRACT: CONTRACT_SCHEMA,
}


# ── Session state ─────────────────────────────────────────────────────────────

@dataclass
class DesignSession:
    """Mutable state for one Studio design session.

    Accumulates step outputs and user clarifications. Thread-safety is the
    caller's responsibility (the FastAPI server uses one session per WebSocket).
    """
    session_id: str
    goal: str
    ctx: StudioContext
    steps: dict[str, Any]       = field(default_factory=dict)
    clarifications: list[str]   = field(default_factory=list)

    @classmethod
    def start(cls, goal: str, ctx: StudioContext) -> "DesignSession":
        return cls(session_id=str(uuid.uuid4())[:8], goal=goal.strip(), ctx=ctx)

    def record(self, step: StepName, result: Any) -> None:
        self.steps[step.value] = result

    def result(self, step: StepName) -> Any | None:
        return self.steps.get(step.value)

    def completed_steps(self) -> list[StepName]:
        return [s for s in StepName if s.value in self.steps]

    def is_design_complete(self) -> bool:
        """True when all five design steps (not CONTRACT) have results."""
        return all(s.value in self.steps for s in DESIGN_STEPS)

    def add_clarification(self, text: str) -> None:
        """Record a user answer to an open_question before re-running a step."""
        if text.strip():
            self.clarifications.append(text.strip())

    def accumulated_context(self, exclude: StepName | None = None) -> str:
        """Previous step outputs serialised for the next step's context argument."""
        parts: list[str] = []
        for step in StepName:
            if step == exclude or step.value not in self.steps or step == StepName.CONTRACT:
                continue
            parts.append(f"[{step.value}]\n{json.dumps(self.steps[step.value], indent=2)}")
        if self.clarifications:
            parts.append("[user_clarifications]\n" + "\n".join(f"- {c}" for c in self.clarifications))
        return "\n\n".join(parts)


# ── Prompt builders ───────────────────────────────────────────────────────────

def _goal_prompt(session: DesignSession) -> str:
    extra = (
        "\n\nThe user has also provided these clarifications:\n"
        + "\n".join(f"- {c}" for c in session.clarifications)
    ) if session.clarifications else ""
    return (
        f"The user wants to build a new AUTOSTATION automation.\n\n"
        f"Their goal: {session.goal}{extra}\n\n"
        "Analyse the goal and output JSON with:\n"
        "- automation_name: kebab-case name starting with 'automation-' "
        "(e.g. automation-cert-renewal)\n"
        "- purpose: one to two sentences describing what it does and why it matters\n"
        "- target_system: the application or service being automated\n"
        "- data_classification: public / internal / confidential / restricted\n"
        "- open_questions: a short list of questions still needed from the user "
        "(empty list if the goal is already clear enough to proceed)\n\n"
        "Be conservative with open_questions — only include questions that would "
        "materially change the design."
    )


def _item_prompt(session: DesignSession) -> str:
    prev = (json.dumps(session.result(StepName.GOAL), indent=2)
            if session.result(StepName.GOAL) else session.goal)
    return (
        "Define the item model for this automation.\n\n"
        "In AUTOSTATION an 'item' is the atomic unit of work in each loop iteration — "
        "one record to process, one file to transform, one certificate to renew. "
        "Every item is logged via wrapper.log_item(item_id, outcome, **kwargs) "
        "with a stable identifier so the dashboard can show per-item success rates.\n\n"
        f"Goal and name from previous step:\n{prev}\n\n"
        "Output JSON with:\n"
        "- item_noun: singular noun for one item (e.g. 'certificate', 'report', 'record')\n"
        "- item_id_field: field name that uniquely identifies one item (e.g. 'cert_id')\n"
        "- item_id_example: a realistic example value (e.g. 'CERT-2024-001')\n"
        "- outcome_categories: possible outcomes (always include 'success', 'failed', 'skipped')\n"
        "- skip_reasons: specific reasons an item is legitimately skipped, not failed\n"
        "- failure_modes: concrete failure scenarios for one item\n"
        "- estimated_items_per_run: rough count range (e.g. '1–20', '50–200')"
    )


def _error_prompt(session: DesignSession) -> str:
    return (
        "Map this automation's failure surface to AUTOSTATION's error classifier.\n\n"
        "AUTOSTATION error classes and their retry behaviour:\n"
        "- NETWORK: VPN drops, connection timeouts, HTTP 5xx → retry after reconnect\n"
        "- DATA_ERROR: missing field, bad format, item not found → skip the item\n"
        "- LOGIC_ERROR: unexpected page state, auth failure, unrecognised output → escalate\n\n"
        f"Automation context so far:\n{session.accumulated_context(exclude=StepName.ERRORS)}\n\n"
        "Output JSON with:\n"
        "- network_scenarios: concrete NETWORK-class failures likely for this automation\n"
        "- data_error_scenarios: concrete DATA_ERROR-class failures\n"
        "- logic_error_scenarios: concrete LOGIC_ERROR-class failures\n"
        "- retry_strategy: plain English description of the overall retry behaviour\n"
        "- max_retries: suggested maximum retries (usually 1–3)"
    )


def _llm_prompt(session: DesignSession) -> str:
    return (
        "Determine whether this automation needs LLM at runtime, and if so, which pattern.\n\n"
        "AUTOSTATION LLM integration patterns:\n"
        "- pattern1: LLM as decision-maker — classify page state or choose next action\n"
        "- pattern2: LLM as output processor — parse, extract or summarise scraped text\n"
        "- pattern3: LLM as browser resilience — recover from unexpected UI state\n"
        "- pattern4: LLM as agentic planner — multi-step planning within a run\n"
        "- none: fully deterministic, no LLM at runtime (most automations)\n\n"
        "LLM calls are opt-in and add latency + cost. Only recommend if the task "
        "genuinely requires language understanding that deterministic code cannot handle.\n\n"
        f"Automation context so far:\n{session.accumulated_context(exclude=StepName.LLM)}\n\n"
        "Output JSON with:\n"
        "- needs_llm: true or false\n"
        "- pattern: the most appropriate pattern or 'none'\n"
        "- pattern_description: what the LLM will do at runtime (empty string if none)\n"
        "- caller_tag: suggested caller= value for llm_call() "
        "(e.g. 'automation-cert-renewal/classify-status')"
    )


def _schedule_prompt(session: DesignSession) -> str:
    return (
        "Define the operational schedule and output configuration.\n\n"
        f"Automation context so far:\n{session.accumulated_context(exclude=StepName.SCHEDULE)}\n\n"
        "Output JSON with:\n"
        "- cron: a cron expression (e.g. '0 6 * * 1' for Monday 6 AM)\n"
        "- cron_human: human-readable schedule description\n"
        "- timeout_s: maximum run duration in seconds "
        "(be generous: include retry time and worst-case item count)\n"
        "- synopsis_target: where the run summary goes — 'teams', 'email', or 'log'\n"
        "- output_description: one sentence describing what the user sees when the run completes"
    )


def _contract_prompt(session: DesignSession) -> str:
    return (
        "Produce the final design contract for this automation.\n\n"
        f"All design decisions:\n{session.accumulated_context(exclude=StepName.CONTRACT)}\n\n"
        "Output JSON with:\n\n"
        "- plain_language: 3–5 paragraphs in plain English describing exactly what this "
        "automation does, when it runs, what it processes, how it handles failures, and "
        "what output it produces. Write for the business owner who will approve it — "
        "no Python, no jargon.\n\n"
        "- developer_tasks: a prioritised list of things the developer must still implement "
        "that cannot be generated (e.g. 'Write the selector for the cert list page', "
        "'Configure CA portal credentials in Credential Manager').\n\n"
        "- project_yaml: a dict ready to write as project.yaml with keys: "
        "name, description, owner (placeholder: 'team@example.com'), "
        "classification, runtime ('python'), version ('0.1.0'), timeout_s.\n\n"
        "- automation_yaml_additions: extra automation.yaml fields beyond project.yaml "
        "defaults: schedule.cron, llm.enabled (bool), synopsis_target. "
        "Omit keys that match defaults."
    )


_PROMPT_BUILDERS = {
    StepName.GOAL:     _goal_prompt,
    StepName.ITEMS:    _item_prompt,
    StepName.ERRORS:   _error_prompt,
    StepName.LLM:      _llm_prompt,
    StepName.SCHEDULE: _schedule_prompt,
    StepName.CONTRACT: _contract_prompt,
}


# ── Runner ────────────────────────────────────────────────────────────────────

class StepError(RuntimeError):
    """LLM step failed — endpoint unavailable or schema mismatch."""


def run_step(session: DesignSession, step: StepName) -> Any:
    """Run one design chain step.

    Records the result in session.steps and returns it.
    Raises StepError if the LLM is unavailable or the response fails schema validation.
    Can be called multiple times on the same step (re-runs replace the previous result).
    """
    from wrapper_core.llm import llm_call  # type: ignore[import]  # on PYTHONPATH via amx

    prompt = _PROMPT_BUILDERS[step](session)
    result = llm_call(
        prompt=prompt,
        context=session.ctx.text,
        schema=STEP_SCHEMAS[step],
        caller=f"studio/{step.value}",
        timeout_s=90,
        template=f"studio_{step.value}",
    )
    if result is None:
        raise StepError(
            f"Step {step.value!r} returned no result — "
            "LLM endpoint unavailable or response failed schema validation"
        )
    session.record(step, result)
    return result


def run_design_chain(session: DesignSession) -> dict[str, Any]:
    """Run all five design steps then produce the contract.

    Returns session.steps. Raises StepError on the first failure.
    For interactive use, call run_step() individually instead.
    """
    for step in (*DESIGN_STEPS, StepName.CONTRACT):
        run_step(session, step)
    return dict(session.steps)
