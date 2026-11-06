"""Tests for automation_studio.chain — interactive design chain."""
import sys
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, call, patch

import pytest

_WC = Path(__file__).resolve().parents[3] / "wrapper-core"
if str(_WC) not in sys.path:
    sys.path.insert(0, str(_WC))

from automation_studio.chain import (
    CONTRACT_SCHEMA,
    DESIGN_STEPS,
    ERROR_SCHEMA,
    GOAL_SCHEMA,
    ITEM_SCHEMA,
    LLM_SCHEMA,
    SCHEDULE_SCHEMA,
    STEP_SCHEMAS,
    DesignSession,
    StepError,
    StepName,
    _error_prompt,
    _goal_prompt,
    _item_prompt,
    _llm_prompt,
    _schedule_prompt,
    _contract_prompt,
    run_design_chain,
    run_step,
)
from automation_studio.context import StudioContext


# ── Fixtures ──────────────────────────────────────────────────────────────────

MOCK_CTX = StudioContext("AUTOSTATION platform context (test)", ["studio_framing"])

GOAL_RESULT = {
    "automation_name": "automation-cert-renewal",
    "purpose": "Renews expiring certificates from the internal CA portal.",
    "target_system": "CA Portal (https://ca.internal/portal)",
    "data_classification": "internal",
    "open_questions": [],
}
ITEM_RESULT = {
    "item_noun": "certificate",
    "item_id_field": "cert_id",
    "item_id_example": "CERT-2024-001",
    "outcome_categories": ["success", "skipped", "failed"],
    "skip_reasons": ["cert_not_due"],
    "failure_modes": ["CA portal unreachable", "cert already expired"],
    "estimated_items_per_run": "1-20",
}
ERROR_RESULT = {
    "network_scenarios": ["VPN drops during cert fetch"],
    "data_error_scenarios": ["cert record missing expiry field"],
    "logic_error_scenarios": ["CA portal UI changed"],
    "retry_strategy": "Retry once after VPN reconnect for NETWORK errors; skip on DATA_ERROR.",
    "max_retries": 1,
}
LLM_RESULT = {
    "needs_llm": False,
    "pattern": "none",
    "pattern_description": "",
    "caller_tag": "",
}
SCHEDULE_RESULT = {
    "cron": "0 6 * * 1",
    "cron_human": "Every Monday at 6 AM",
    "timeout_s": 1800,
    "synopsis_target": "teams",
    "output_description": "Posts a renewal summary to the Teams channel.",
}
CONTRACT_RESULT = {
    "plain_language": "This automation runs every Monday at 6 AM and renews certificates...",
    "developer_tasks": ["Implement selector for cert list page", "Configure CA credentials"],
    "project_yaml": {
        "name": "automation-cert-renewal",
        "description": "Renews expiring certificates from the internal CA portal.",
        "owner": "team@example.com",
        "classification": "internal",
        "runtime": "python",
        "version": "0.1.0",
        "timeout_s": 1800,
    },
    "automation_yaml_additions": {"schedule": {"cron": "0 6 * * 1"}},
}


def _make_session(**kwargs) -> DesignSession:
    return DesignSession.start("Renew expiring certs from the CA portal", MOCK_CTX, **kwargs)


# ── DesignSession ─────────────────────────────────────────────────────────────

def test_session_start_sets_goal():
    s = _make_session()
    assert s.goal == "Renew expiring certs from the CA portal"


def test_session_start_generates_session_id():
    s1, s2 = _make_session(), _make_session()
    assert s1.session_id != s2.session_id
    assert len(s1.session_id) == 8


def test_session_start_strips_goal():
    s = DesignSession.start("  my goal  ", MOCK_CTX)
    assert s.goal == "my goal"


def test_session_record_and_result():
    s = _make_session()
    s.record(StepName.GOAL, GOAL_RESULT)
    assert s.result(StepName.GOAL) == GOAL_RESULT


def test_session_result_missing_step_returns_none():
    s = _make_session()
    assert s.result(StepName.GOAL) is None


def test_session_record_overwrites_previous():
    s = _make_session()
    s.record(StepName.GOAL, {"automation_name": "v1"})
    s.record(StepName.GOAL, {"automation_name": "v2"})
    assert s.result(StepName.GOAL)["automation_name"] == "v2"


def test_session_completed_steps_empty_at_start():
    s = _make_session()
    assert s.completed_steps() == []


def test_session_completed_steps_after_recording():
    s = _make_session()
    s.record(StepName.GOAL, GOAL_RESULT)
    s.record(StepName.ITEMS, ITEM_RESULT)
    assert StepName.GOAL in s.completed_steps()
    assert StepName.ITEMS in s.completed_steps()
    assert StepName.ERRORS not in s.completed_steps()


def test_session_is_design_complete_false_initially():
    assert not _make_session().is_design_complete()


def test_session_is_design_complete_true_after_all_steps():
    s = _make_session()
    for step, result in zip(
        DESIGN_STEPS,
        [GOAL_RESULT, ITEM_RESULT, ERROR_RESULT, LLM_RESULT, SCHEDULE_RESULT],
    ):
        s.record(step, result)
    assert s.is_design_complete()


def test_session_is_design_complete_false_missing_one():
    s = _make_session()
    for step, result in zip(
        [StepName.GOAL, StepName.ITEMS, StepName.ERRORS, StepName.LLM],
        [GOAL_RESULT, ITEM_RESULT, ERROR_RESULT, LLM_RESULT],
    ):
        s.record(step, result)
    assert not s.is_design_complete()  # SCHEDULE missing


def test_session_add_clarification():
    s = _make_session()
    s.add_clarification("The cert portal URL is https://ca.internal")
    assert len(s.clarifications) == 1
    assert "ca.internal" in s.clarifications[0]


def test_session_add_clarification_ignores_blank():
    s = _make_session()
    s.add_clarification("   ")
    assert s.clarifications == []


def test_session_accumulated_context_empty_at_start():
    s = _make_session()
    assert s.accumulated_context() == ""


def test_session_accumulated_context_includes_step_results():
    s = _make_session()
    s.record(StepName.GOAL, GOAL_RESULT)
    ctx = s.accumulated_context()
    assert "goal_refinement" in ctx
    assert "automation-cert-renewal" in ctx


def test_session_accumulated_context_excludes_contract():
    s = _make_session()
    s.record(StepName.CONTRACT, CONTRACT_RESULT)
    assert "contract" not in s.accumulated_context()


def test_session_accumulated_context_includes_clarifications():
    s = _make_session()
    s.record(StepName.GOAL, GOAL_RESULT)
    s.add_clarification("The portal uses SAML auth")
    ctx = s.accumulated_context()
    assert "SAML auth" in ctx


def test_session_accumulated_context_excludes_given_step():
    s = _make_session()
    s.record(StepName.GOAL, GOAL_RESULT)
    s.record(StepName.ITEMS, ITEM_RESULT)
    ctx = s.accumulated_context(exclude=StepName.ITEMS)
    assert "goal_refinement" in ctx
    assert "item_model" not in ctx


# ── Schemas ───────────────────────────────────────────────────────────────────

def test_step_schemas_covers_all_steps():
    for step in StepName:
        assert step in STEP_SCHEMAS, f"Missing schema for {step}"


def test_goal_schema_has_required_keys():
    assert "automation_name" in GOAL_SCHEMA
    assert "open_questions" in GOAL_SCHEMA
    assert "data_classification" in GOAL_SCHEMA


def test_contract_schema_has_required_keys():
    assert "plain_language" in CONTRACT_SCHEMA
    assert "developer_tasks" in CONTRACT_SCHEMA
    assert "project_yaml" in CONTRACT_SCHEMA


# ── Prompt builders ───────────────────────────────────────────────────────────

def test_goal_prompt_contains_goal():
    s = _make_session()
    prompt = _goal_prompt(s)
    assert s.goal in prompt


def test_goal_prompt_includes_clarifications():
    s = _make_session()
    s.add_clarification("The portal uses LDAP auth")
    prompt = _goal_prompt(s)
    assert "LDAP auth" in prompt


def test_goal_prompt_no_clarifications_section_when_empty():
    s = _make_session()
    prompt = _goal_prompt(s)
    assert "clarifications" not in prompt.lower()


def test_item_prompt_includes_previous_goal():
    s = _make_session()
    s.record(StepName.GOAL, GOAL_RESULT)
    prompt = _item_prompt(s)
    assert "automation-cert-renewal" in prompt


def test_item_prompt_uses_raw_goal_when_no_step_result():
    s = _make_session()
    prompt = _item_prompt(s)
    assert s.goal in prompt


def test_error_prompt_includes_accumulated_context():
    s = _make_session()
    s.record(StepName.GOAL, GOAL_RESULT)
    s.record(StepName.ITEMS, ITEM_RESULT)
    prompt = _error_prompt(s)
    assert "automation-cert-renewal" in prompt
    assert "certificate" in prompt


def test_error_prompt_excludes_own_step_in_context():
    s = _make_session()
    s.record(StepName.ERRORS, ERROR_RESULT)
    prompt = _error_prompt(s)
    assert "error_classes" not in prompt


def test_llm_prompt_mentions_patterns():
    s = _make_session()
    prompt = _llm_prompt(s)
    for p in ("pattern1", "pattern2", "pattern3", "pattern4"):
        assert p in prompt


def test_schedule_prompt_mentions_cron():
    s = _make_session()
    prompt = _schedule_prompt(s)
    assert "cron" in prompt.lower()


def test_contract_prompt_requests_plain_language():
    s = _make_session()
    prompt = _contract_prompt(s)
    assert "plain" in prompt.lower()
    assert "developer_tasks" in prompt


# ── run_step ──────────────────────────────────────────────────────────────────

def test_run_step_records_result():
    s = _make_session()
    with patch("wrapper_core.llm.llm_call", return_value=GOAL_RESULT):
        result = run_step(s, StepName.GOAL)
    assert result == GOAL_RESULT
    assert s.result(StepName.GOAL) == GOAL_RESULT


def test_run_step_raises_on_none_result():
    s = _make_session()
    with patch("wrapper_core.llm.llm_call", return_value=None):
        with pytest.raises(StepError, match="goal_refinement"):
            run_step(s, StepName.GOAL)


def test_run_step_does_not_record_on_failure():
    s = _make_session()
    with patch("wrapper_core.llm.llm_call", return_value=None):
        with pytest.raises(StepError):
            run_step(s, StepName.GOAL)
    assert s.result(StepName.GOAL) is None


def test_run_step_passes_caller_tag():
    s = _make_session()
    with patch("wrapper_core.llm.llm_call", return_value=GOAL_RESULT) as mock_llm:
        run_step(s, StepName.GOAL)
    _, kwargs = mock_llm.call_args
    assert kwargs.get("caller") == "studio/goal_refinement"


def test_run_step_passes_context():
    s = _make_session()
    with patch("wrapper_core.llm.llm_call", return_value=GOAL_RESULT) as mock_llm:
        run_step(s, StepName.GOAL)
    _, kwargs = mock_llm.call_args
    assert kwargs.get("context") == MOCK_CTX.text


def test_run_step_passes_schema():
    s = _make_session()
    with patch("wrapper_core.llm.llm_call", return_value=ITEM_RESULT) as mock_llm:
        run_step(s, StepName.ITEMS)
    _, kwargs = mock_llm.call_args
    assert kwargs.get("schema") == ITEM_SCHEMA


def test_run_step_can_rerun_to_update_result():
    s = _make_session()
    with patch("wrapper_core.llm.llm_call", return_value={"automation_name": "v1", **GOAL_RESULT}):
        run_step(s, StepName.GOAL)
    updated = {**GOAL_RESULT, "automation_name": "automation-cert-renewal-v2"}
    with patch("wrapper_core.llm.llm_call", return_value=updated):
        run_step(s, StepName.GOAL)
    assert s.result(StepName.GOAL)["automation_name"] == "automation-cert-renewal-v2"


# ── run_design_chain ──────────────────────────────────────────────────────────

def test_run_design_chain_runs_all_steps():
    s = _make_session()
    side_effects = [GOAL_RESULT, ITEM_RESULT, ERROR_RESULT, LLM_RESULT, SCHEDULE_RESULT, CONTRACT_RESULT]
    with patch("wrapper_core.llm.llm_call", side_effect=side_effects):
        result = run_design_chain(s)
    assert set(result.keys()) == {step.value for step in StepName}


def test_run_design_chain_steps_in_order():
    s = _make_session()
    side_effects = [GOAL_RESULT, ITEM_RESULT, ERROR_RESULT, LLM_RESULT, SCHEDULE_RESULT, CONTRACT_RESULT]
    call_callers: list[str] = []

    def capture_call(*args, **kwargs):
        call_callers.append(kwargs.get("caller", ""))
        return side_effects[len(call_callers) - 1]

    with patch("wrapper_core.llm.llm_call", side_effect=capture_call):
        run_design_chain(s)

    assert call_callers == [
        "studio/goal_refinement",
        "studio/item_model",
        "studio/error_classes",
        "studio/llm_integration",
        "studio/schedule_output",
        "studio/contract",
    ]


def test_run_design_chain_raises_on_step_failure():
    s = _make_session()
    with patch("wrapper_core.llm.llm_call", return_value=None):
        with pytest.raises(StepError):
            run_design_chain(s)


def test_run_design_chain_contract_has_project_yaml():
    s = _make_session()
    side_effects = [GOAL_RESULT, ITEM_RESULT, ERROR_RESULT, LLM_RESULT, SCHEDULE_RESULT, CONTRACT_RESULT]
    with patch("wrapper_core.llm.llm_call", side_effect=side_effects):
        run_design_chain(s)
    contract = s.result(StepName.CONTRACT)
    assert "project_yaml" in contract
    assert contract["project_yaml"]["name"] == "automation-cert-renewal"


def test_run_design_chain_session_is_complete_after():
    s = _make_session()
    side_effects = [GOAL_RESULT, ITEM_RESULT, ERROR_RESULT, LLM_RESULT, SCHEDULE_RESULT, CONTRACT_RESULT]
    with patch("wrapper_core.llm.llm_call", side_effect=side_effects):
        run_design_chain(s)
    assert s.is_design_complete()
