from __future__ import annotations

from typing import Any, Dict, Optional

from .domain import ConflictError, ensure_role, normalize_severity, require_number, require_text
from .repository import Repository
from .rules import (AUDIT_ROLES, CREATE_ROLES, ENTITY, GATED_TRANSITIONS,
                    PLAN_CONFIRM_ROLES, PLAN_SAVE_ROLES, RECORD_ROLES, TITLE,
                    VIEW_ROLES, completion_blockers, escalation_required,
                    plan_gate_blockers, priority_score, response_deadline_hours,
                    role_for_transition, validate_transition)


class Service:
    def __init__(self, repository: Repository):
        self.repository = repository

    def _view(self, role: str) -> None:
        ensure_role(role, VIEW_ROLES)

    def create_item(self, payload: Dict[str, Any], actor: str, role: str) -> Dict[str, Any]:
        ensure_role(role, CREATE_ROLES)
        actor = require_text(actor, "actor", 100)
        title = require_text(payload.get("title"), "title", 200)
        description = require_text(payload.get("description"), "description")
        severity = normalize_severity(payload.get("severity"))
        quantity = require_number(payload.get("quantity", 0), "quantity")
        threshold = require_number(payload.get("threshold", 1), "threshold", 0.000001)
        external_ref = payload.get("external_ref")
        if external_ref is not None:
            external_ref = require_text(external_ref, "external_ref", 100)
        item = self.repository.create_item(title, description, severity, quantity,
                                           threshold, external_ref, actor)
        self.repository.append_audit("create", ENTITY, item["id"], actor, {
            "title": title, "severity": severity, "quantity": quantity,
            "priority": priority_score(severity, quantity, threshold),
        })
        return self.enrich(item)

    def add_record(self, item_id: int, payload: Dict[str, Any], actor: str,
                   role: str) -> Dict[str, Any]:
        ensure_role(role, RECORD_ROLES)
        actor = require_text(actor, "actor", 100)
        kind = require_text(payload.get("kind"), "kind", 100)
        detail = require_text(payload.get("detail"), "detail")
        status = payload.get("status", "open")
        if status not in ("open", "closed"):
            raise ValueError("status必须是open或closed")
        external_ref = payload.get("external_ref")
        if external_ref is not None:
            external_ref = require_text(external_ref, "external_ref", 100)
        record = self.repository.add_record(item_id, kind, detail, status,
                                            external_ref, actor)
        self.repository.append_audit("record", ENTITY, item_id, actor, {
            "record_id": record["id"], "kind": kind, "status": status,
        })
        return record

    def transition(self, item_id: int, target: str, expected_version: int,
                   actor: str, role: str) -> Dict[str, Any]:
        actor = require_text(actor, "actor", 100)
        item = self.repository.get_item(item_id)
        validate_transition(item["status"], target)
        ensure_role(role, role_for_transition(target))
        if not isinstance(expected_version, int) or expected_version < 1:
            raise ValueError("expected_version必须是正整数")
        blockers = completion_blockers(target, self.repository.open_record_count(item_id))
        if blockers:
            raise ConflictError("；".join(blockers))
        escalation = escalation_required(
            item["severity"], item["quantity"], item["threshold"])
        gated = (item["status"], target) in GATED_TRANSITIONS and escalation
        summary = None
        if gated:
            summary = self._plan_summary(item_id)
            blockers = plan_gate_blockers(
                True, summary["version"],
                {c["role"] for c in summary["confirmations"]})
            if blockers:
                raise ConflictError("；".join(blockers))
        updated = self.repository.transition_item(item_id, target, expected_version, actor)
        self.repository.append_audit("transition", ENTITY, item_id, actor, {
            "from": item["status"], "to": target,
            "escalation_required": escalation,
        })
        if gated:
            self.repository.append_audit("release", ENTITY, item_id, actor, {
                "from": item["status"], "to": target,
                "plan_version": summary["version"],
                "confirmed_by": [c["actor"] for c in summary["confirmations"]],
            })
        return self.enrich(updated)

    def save_plan(self, item_id: int, payload: Dict[str, Any], actor: str,
                  role: str) -> Dict[str, Any]:
        ensure_role(role, PLAN_SAVE_ROLES)
        actor = require_text(actor, "actor", 100)
        steps = require_text(payload.get("steps"), "steps")
        evacuation_scope = require_text(payload.get("evacuation_scope"), "evacuation_scope", 500)
        self.repository.get_item(item_id)
        plan = self.repository.create_plan_version(item_id, steps, evacuation_scope, actor)
        self.repository.append_audit("plan_save", ENTITY, item_id, actor, {
            "plan_version": plan["version"], "steps": steps,
            "evacuation_scope": evacuation_scope,
        })
        return self._plan_summary(item_id)

    def confirm_plan(self, item_id: int, actor: str, role: str) -> Dict[str, Any]:
        ensure_role(role, PLAN_CONFIRM_ROLES)
        actor = require_text(actor, "actor", 100)
        self.repository.get_item(item_id)
        plan = self.repository.get_current_plan(item_id)
        if plan is None:
            raise ConflictError("尚未保存应急预案，无法确认")
        self.repository.confirm_plan(item_id, plan["version"], role, actor)
        self.repository.append_audit("plan_confirm", ENTITY, item_id, actor, {
            "plan_version": plan["version"], "role": role,
        })
        return self._plan_summary(item_id)

    def plan_status(self, item_id: int, role: str) -> Dict[str, Any]:
        self._view(role)
        self.repository.get_item(item_id)
        return self._plan_summary(item_id)

    def _plan_summary(self, item_id: int) -> Dict[str, Any]:
        plan = self.repository.get_current_plan(item_id)
        confirmations = (self.repository.list_confirmations(item_id, plan["version"])
                         if plan else [])
        confirmed_roles = {c["role"] for c in confirmations}
        pending = [r for r in PLAN_CONFIRM_ROLES if r not in confirmed_roles]
        return {
            "version": plan["version"] if plan else None,
            "steps": plan["steps"] if plan else None,
            "evacuation_scope": plan["evacuation_scope"] if plan else None,
            "updated_by": plan["created_by"] if plan else None,
            "updated_at": plan["created_at"] if plan else None,
            "confirmations": [{"role": c["role"], "actor": c["actor"],
                               "created_at": c["created_at"]} for c in confirmations],
            "pending_roles": pending,
            "complete": plan is not None and not pending,
        }

    def get_item(self, item_id: int, role: str) -> Dict[str, Any]:
        self._view(role)
        return self.enrich(self.repository.get_item(item_id))

    def list_items(self, role: str, status: Optional[str] = None) -> list:
        self._view(role)
        return [self.enrich(item) for item in self.repository.list_items(status)]

    def list_records(self, item_id: int, role: str) -> list:
        self._view(role)
        return self.repository.list_records(item_id)

    def audit(self, role: str, item_id: Optional[int] = None) -> list:
        ensure_role(role, AUDIT_ROLES)
        return self.repository.list_audit(item_id)

    def enrich(self, item: Dict[str, Any]) -> Dict[str, Any]:
        result = dict(item)
        result["priority"] = priority_score(
            item["severity"], item["quantity"], item["threshold"])
        result["deadline_hours"] = response_deadline_hours(
            item["severity"], item["quantity"], item["threshold"])
        result["escalation_required"] = escalation_required(
            item["severity"], item["quantity"], item["threshold"])
        summary = self._plan_summary(item["id"])
        result["plan"] = summary
        result["repair_blockers"] = (
            plan_gate_blockers(result["escalation_required"], summary["version"],
                               {c["role"] for c in summary["confirmations"]})
            if item["status"] == "defect_confirmed" else [])
        return result
