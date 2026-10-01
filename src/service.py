"""业务用例编排、权限检查与审计。"""
import uuid
from typing import Any, Dict, List, Optional

from .audit import AuditRecorder
from .domain import Actor, PermissionDenied, integer, optional_text, text
from .repository import Repository
from .rules import DomainRules, optional_datetime


class Service:
    def __init__(self, repository: Repository, rules: DomainRules, audit: AuditRecorder = None) -> None:
        self.repository = repository
        self.rules = rules
        self.audit = audit or AuditRecorder(repository)

    @staticmethod
    def _actor(actor: Actor) -> Actor:
        if actor is None or not actor.user_id.strip() or not actor.role.strip():
            raise PermissionDenied("缺少调用身份")
        return actor

    def _ensure_known_role(self, actor: Actor) -> None:
        if not self.rules.known_role(actor.role):
            raise PermissionDenied("角色无权访问该服务")

    def _require_action_role(self, actor: Actor, action: str) -> None:
        if not self.rules.role_can_action(actor.role, action):
            raise PermissionDenied("角色无权执行该操作")

    def create(self, actor: Actor, reference: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        if not self.rules.role_can_create(actor.role):
            raise PermissionDenied("角色无权创建记录")
        reference = text({"reference": reference}, "reference")
        prepared = self.rules.prepare_create(payload or {})
        self.rules.check_create_conflicts(prepared, self.repository.list_records(limit=500))
        return self.repository.create(reference, self.rules.INITIAL_STATE, prepared, actor.user_id)

    def list_records(self, actor: Actor, state: Optional[str] = None, limit: int = 100) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.list_records(state=state, limit=limit)

    def get_record(self, actor: Actor, record_id: int) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.get(record_id)

    def act(self, actor: Actor, record_id: int, expected_version: int, action: str, data: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        action = text({"action": action}, "action")
        self._require_action_role(actor, action)
        record = self.repository.get(record_id)
        request_id = optional_text(data or {}, "request_id", "")
        if action == "settle" and record["state"] == "settled" and request_id and record["payload"].get("settlement_request_id") == request_id:
            return record
        new_state, new_payload, summary = self.rules.apply_action(record, action, data or {})
        details = {"summary": summary, "input": data or {}, "from": record["state"], "to": new_state}
        if action == "settle":
            return self.repository.settle_instruction(
                record_id=record_id,
                expected_version=int(expected_version),
                payload=new_payload,
                delivered_quantity=int(new_payload["delivered_quantity"]),
                cash_paid=float(new_payload["cash_paid"]),
                settled_at=new_payload["settled_at"],
                request_id=new_payload.get("settlement_request_id"),
                actor_id=actor.user_id,
                details=details,
            )
        if action == "fail":
            return self.repository.fail_instruction(
                record_id=record_id,
                expected_version=int(expected_version),
                payload=new_payload,
                reason=new_payload["fail_reason"],
                actor_id=actor.user_id,
                details=details,
            )
        return self.repository.mutate(
            record_id=record_id,
            expected_version=int(expected_version),
            state=new_state,
            payload=new_payload,
            actor_id=actor.user_id,
            action=action,
            details=details,
        )

    def create_position(self, actor: Actor, payload: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        self._require_action_role(actor, "create_position")
        data = payload or {}
        instrument = text(data, "instrument")
        account_id = optional_text(data, "account_id", "DEFAULT")
        quantity = integer(data, "quantity", 0)
        recorded_at = optional_datetime(data, "recorded_at")
        return self.repository.create_position(instrument, account_id, quantity, recorded_at, actor.user_id)

    def list_positions(self, actor: Actor, instrument: Optional[str] = None, limit: int = 100) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.list_positions(instrument=instrument, limit=limit)

    def get_position(self, actor: Actor, instrument: str, account_id: str = "DEFAULT") -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.get_position(instrument, account_id)

    def adjust_position(self, actor: Actor, payload: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        self._require_action_role(actor, "adjust_position")
        data = payload or {}
        instrument = text(data, "instrument")
        account_id = optional_text(data, "account_id", "DEFAULT")
        delta = integer(data, "delta")
        if delta == 0:
            from .domain import ValidationError
            raise ValidationError("delta不能为0")
        recorded_at = optional_datetime(data, "recorded_at")
        if recorded_at is None:
            from .domain import ValidationError
            raise ValidationError("recorded_at不能为空")
        reason = text(data, "reason")
        request_id = optional_text(data, "request_id", "position-%s" % uuid.uuid4())
        return self.repository.adjust_position(
            instrument=instrument,
            account_id=account_id,
            delta=delta,
            recorded_at=recorded_at,
            request_id=request_id,
            reason=reason,
            actor_id=actor.user_id,
        )

    def create_corporate_action(self, actor: Actor, reference: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        self._require_action_role(actor, "create_corporate_action")
        reference = text({"reference": reference}, "reference")
        prepared = self.rules.prepare_corporate_action(payload or {})
        return self.repository.create_corporate_action(reference, prepared, actor.user_id)

    def get_corporate_action(self, actor: Actor, corporate_action_id: int) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.get_corporate_action(corporate_action_id)

    def list_corporate_actions(
        self,
        actor: Actor,
        instrument: Optional[str] = None,
        status: Optional[str] = None,
        limit: int = 100,
    ) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.list_corporate_actions(instrument=instrument, status=status, limit=limit)

    def freeze_corporate_action(self, actor: Actor, corporate_action_id: int, expected_version: int, data: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        self._require_action_role(actor, "freeze_corporate_action")
        request_id = text(data or {}, "request_id")
        return self.repository.freeze_corporate_action(
            corporate_action_id, int(expected_version), request_id, actor.user_id
        )

    def issue_corporate_action(self, actor: Actor, corporate_action_id: int, expected_version: int, data: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        self._require_action_role(actor, "issue_corporate_action")
        request_id = text(data or {}, "request_id")
        return self.repository.issue_corporate_action(
            corporate_action_id, int(expected_version), request_id, actor.user_id
        )

    def list_entitlements(self, actor: Actor, corporate_action_id: int, status: Optional[str] = None) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.list_entitlements(corporate_action_id, status=status)

    def list_instruction_reviews(self, actor: Actor, corporate_action_id: int, status: Optional[str] = None) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.list_instruction_reviews(corporate_action_id, status=status)

    def timeline(self, actor: Actor, record_id: int) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.audit.timeline(record_id)

    def stats(self, actor: Actor) -> Dict[str, int]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.stats()
