"""Candidate-first skill evolution workflow.

This module implements the minimal automatic skill update loop:
1. Attribute a failed execution to skill / router / environment / context
2. If the failure belongs to the skill, generate a revised SKILL.md
3. Validate the candidate statically
4. Store it as a candidate instead of overwriting the live skill
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel

from core.memento_s.utils import extract_json
from core.prompts.templates import SKILL_ATTRIBUTION_PROMPT, SKILL_PATCH_PROMPT
from core.skill.schema import Skill
from middleware.config import g_config
from utils.logger import get_logger

logger = get_logger(__name__)


class AttributionResult(BaseModel):
    failure_owner: str
    should_edit_skill: bool = False
    confidence: float = 0.0
    reason: str = ""
    suggested_focus: str = ""


class PatchResult(BaseModel):
    changed: bool = False
    summary: str = ""
    updated_skill_md: str = ""


async def attempt_skill_evolution(
    *,
    skill: Skill | None,
    task: str,
    summary: str,
    output: Any,
    diagnostics: dict[str, Any] | None,
    llm: Any,
    store: Any,
    replay_params: dict[str, Any] | None = None,
    replay_execute: Any | None = None,
) -> dict[str, Any]:
    """Try to generate and store a candidate skill update after failure."""
    evolution_cfg = getattr(g_config.skills, "evolution", None)
    if evolution_cfg is not None and not evolution_cfg.enabled:
        return {
            "attempted": False,
            "status": "skipped",
            "reason": "skill_evolution_disabled",
        }

    if skill is None:
        return {
            "attempted": False,
            "status": "skipped",
            "reason": "skill_not_found_locally",
        }

    if not skill.source_dir:
        return {
            "attempted": False,
            "status": "skipped",
            "reason": "skill_has_no_source_dir",
        }

    try:
        attribution = await _run_attribution(
            skill=skill,
            task=task,
            summary=summary,
            output=output,
            diagnostics=diagnostics or {},
            llm=llm,
        )
    except Exception as e:
        logger.warning("Skill attribution failed for '{}': {}", skill.name, e)
        return {
            "attempted": False,
            "status": "judge_error",
            "reason": str(e),
        }

    if not attribution.should_edit_skill or attribution.failure_owner != "skill_fault":
        return {
            "attempted": True,
            "status": "not_editable",
            "failure_owner": attribution.failure_owner,
            "confidence": attribution.confidence,
            "reason": attribution.reason,
        }

    try:
        patch = await _run_patch(
            skill=skill,
            task=task,
            summary=summary,
            output=output,
            diagnostics=diagnostics or {},
            attribution=attribution,
            llm=llm,
        )
    except Exception as e:
        logger.warning("Skill patch generation failed for '{}': {}", skill.name, e)
        return {
            "attempted": True,
            "status": "patch_error",
            "failure_owner": attribution.failure_owner,
            "reason": str(e),
        }

    if not patch.changed or not patch.updated_skill_md.strip():
        return {
            "attempted": True,
            "status": "no_change",
            "failure_owner": attribution.failure_owner,
            "reason": patch.summary or attribution.reason,
        }

    try:
        candidate = await store.store_candidate(
            skill=skill,
            updated_skill_md=patch.updated_skill_md,
            metadata={
                "task": task,
                "summary": summary,
                "diagnostics": diagnostics or {},
                "attribution": attribution.model_dump(),
                "patch_summary": patch.summary,
            },
        )
    except Exception as e:
        logger.warning("Candidate storage failed for '{}': {}", skill.name, e)
        return {
            "attempted": True,
            "status": "store_error",
            "failure_owner": attribution.failure_owner,
            "reason": str(e),
        }

    verify_result = await store.verify_candidate(
        candidate_path=str(candidate["path"]),
        parent_skill_name=skill.name,
    )
    if not verify_result.get("ok"):
        await store.reject_candidate(
            candidate_path=str(candidate["path"]),
            reason=str(verify_result.get("reason", "verification_failed")),
        )
        return {
            "attempted": True,
            "status": "candidate_rejected",
            "failure_owner": attribution.failure_owner,
            "confidence": attribution.confidence,
            "reason": attribution.reason,
            "patch_summary": patch.summary,
            "verification": verify_result,
            "candidate": candidate,
        }

    replay_result = {
        "ok": False,
        "reason": "replay_not_configured",
    }
    if replay_execute is not None:
        try:
            candidate_skill = store.load_candidate(str(candidate["path"]))
            replay_envelope = await replay_execute(
                candidate_skill,
                replay_params or {"request": task},
            )
            replay_result = {
                "ok": bool(replay_envelope.ok),
                "status": replay_envelope.status.value,
                "summary": replay_envelope.summary,
            }
        except Exception as e:
            replay_result = {
                "ok": False,
                "reason": f"replay_error: {e}",
            }

    if replay_execute is not None and not replay_result.get("ok"):
        await store.reject_candidate(
            candidate_path=str(candidate["path"]),
            reason=str(replay_result.get("reason") or replay_result.get("summary") or "targeted_replay_failed"),
        )
        return {
            "attempted": True,
            "status": "candidate_rejected",
            "failure_owner": attribution.failure_owner,
            "confidence": attribution.confidence,
            "reason": attribution.reason,
            "patch_summary": patch.summary,
            "verification": verify_result,
            "replay": replay_result,
            "candidate": candidate,
        }

    regression_result = {
        "ok": True,
        "reason": "no_baseline_samples",
        "checked": 0,
        "failures": [],
    }
    if replay_execute is not None:
        examples = store.get_recent_success_examples(skill_name=skill.name, limit=3)
        if examples:
            candidate_skill = store.load_candidate(str(candidate["path"]))
            failures: list[dict[str, Any]] = []
            checked = 0
            for ex in examples:
                request = str(ex.get("request", "")).strip()
                if not request:
                    continue
                checked += 1
                try:
                    replay_envelope = await replay_execute(
                        candidate_skill,
                        {"request": request},
                    )
                    if not replay_envelope.ok:
                        failures.append(
                            {
                                "request": request,
                                "summary": replay_envelope.summary,
                            }
                        )
                except Exception as e:
                    failures.append({"request": request, "summary": str(e)})

            regression_result = {
                "ok": len(failures) == 0,
                "reason": "regression_replay_passed" if len(failures) == 0 else "regression_replay_failed",
                "checked": checked,
                "failures": failures,
            }

    if not regression_result.get("ok"):
        await store.reject_candidate(
            candidate_path=str(candidate["path"]),
            reason=str(regression_result.get("reason", "regression_replay_failed")),
        )
        return {
            "attempted": True,
            "status": "candidate_rejected",
            "failure_owner": attribution.failure_owner,
            "confidence": attribution.confidence,
            "reason": attribution.reason,
            "patch_summary": patch.summary,
            "verification": verify_result,
            "replay": replay_result,
            "regression": regression_result,
            "candidate": candidate,
        }

    auto_promote_enabled = bool(
        getattr(evolution_cfg, "auto_promote_enabled", False)
    ) if evolution_cfg is not None else False
    auto_promote_min_confidence = float(
        getattr(evolution_cfg, "auto_promote_min_confidence", 0.95)
    ) if evolution_cfg is not None else 0.95

    if auto_promote_enabled and attribution.confidence >= auto_promote_min_confidence:
        promoted = await store.promote_candidate(candidate_path=str(candidate["path"]))
        return {
            "attempted": True,
            "status": "promoted",
            "failure_owner": attribution.failure_owner,
            "confidence": attribution.confidence,
            "reason": attribution.reason,
            "patch_summary": patch.summary,
            "verification": verify_result,
            "replay": replay_result,
            "regression": regression_result,
            "candidate": candidate,
            "promoted": promoted,
        }

    return {
        "attempted": True,
        "status": "candidate_created",
        "failure_owner": attribution.failure_owner,
        "confidence": attribution.confidence,
        "reason": attribution.reason,
        "patch_summary": patch.summary,
        "verification": verify_result,
        "replay": replay_result,
        "regression": regression_result,
        "candidate": candidate,
    }


async def _run_attribution(
    *,
    skill: Skill,
    task: str,
    summary: str,
    output: Any,
    diagnostics: dict[str, Any],
    llm: Any,
) -> AttributionResult:
    prompt = SKILL_ATTRIBUTION_PROMPT.format(
        task=task or "(missing task)",
        skill_name=skill.name,
        skill_description=skill.description or "",
        skill_content=skill.content,
        summary=summary or "",
        output=_truncate_text(output),
        diagnostics=_truncate_text(diagnostics),
    )
    resp = await llm.async_chat(
        messages=[{"role": "user", "content": prompt}],
        temperature=0,
        max_tokens=900,
    )
    data = extract_json((resp.content or "").strip())
    return AttributionResult(**data)


async def _run_patch(
    *,
    skill: Skill,
    task: str,
    summary: str,
    output: Any,
    diagnostics: dict[str, Any],
    attribution: AttributionResult,
    llm: Any,
) -> PatchResult:
    prompt = SKILL_PATCH_PROMPT.format(
        task=task or "(missing task)",
        skill_name=skill.name,
        skill_description=skill.description or "",
        skill_content=skill.content,
        summary=summary or "",
        output=_truncate_text(output),
        diagnostics=_truncate_text(diagnostics),
        reason=attribution.reason,
        focus=attribution.suggested_focus,
    )
    resp = await llm.async_chat(
        messages=[{"role": "user", "content": prompt}],
        temperature=0.2,
        max_tokens=2200,
    )
    data = extract_json((resp.content or "").strip())
    return PatchResult(**data)


def _truncate_text(value: Any, limit: int = 5000) -> str:
    text = str(value or "")
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."
