"""业务用例编排、权限检查与审计。"""
from typing import Any, Dict, List, Optional

from .audit import AuditRecorder
from .domain import Actor, Conflict, PermissionDenied, ValidationError, text
from .repository import Repository
from .rules import DomainRules


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

    # ---------- 结算指令 ----------
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
        if not self.rules.role_can_action(actor.role, action):
            raise PermissionDenied("角色无权执行该操作")
        record = self.repository.get(record_id)
        new_state, new_payload, summary = self.rules.apply_action(record, action, data or {})
        details = {"summary": summary, "input": data or {}, "from": record["state"], "to": new_state}
        if action == "settle":
            # 交收定稿、双腿过户、未定稿权益失效在同一事务内完成
            legs = self.rules.settlement_legs(new_payload)
            return self.repository.settle_instruction(
                record=record,
                expected_version=int(expected_version),
                payload=new_payload,
                actor_id=actor.user_id,
                details=details,
                legs=legs,
                settled_at=new_payload["settled_at"],
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

    def timeline(self, actor: Actor, record_id: int) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.audit.timeline(record_id)

    def stats(self, actor: Actor) -> Dict[str, int]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.stats()

    # ---------- 持仓 ----------
    def create_holding(self, actor: Actor, reference: str = None, payload: Dict[str, Any] = None) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        if not self.rules.role_can_manage_holdings(actor.role):
            raise PermissionDenied("角色无权维护持仓")
        prepared = self.rules.validate_holding(payload or {})
        return self.repository.create_holding(
            account=prepared["account"],
            instrument=prepared["instrument"],
            quantity=prepared["quantity"],
            recorded_at=prepared["recorded_at"],
            actor_id=actor.user_id,
        )

    def list_holdings(self, actor: Actor, instrument: Optional[str] = None, account: Optional[str] = None, limit: int = 100) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.list_holdings(instrument=instrument, account=account, limit=limit)

    def get_holding(self, actor: Actor, holding_id: int) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.get_holding(holding_id)

    def adjust_holding(self, actor: Actor, holding_id: int, expected_version: int, payload: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        if not self.rules.role_can_manage_holdings(actor.role):
            raise PermissionDenied("角色无权维护持仓")
        delta, reason = self.rules.validate_adjustment(payload or {})
        return self.repository.adjust_holding(holding_id, int(expected_version), delta, reason, actor.user_id)

    def holding_timeline(self, actor: Actor, holding_id: int) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        self.repository.get_holding(holding_id)
        return self.repository.subject_timeline("holding", holding_id)

    # ---------- 公司行动与权益 ----------
    def create_corporate_action(self, actor: Actor, reference: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        if not self.rules.role_can_manage_corporate_actions(actor.role):
            raise PermissionDenied("角色无权维护公司行动")
        reference = text({"reference": reference}, "reference")
        prepared = self.rules.validate_corporate_action(payload or {})
        return self.repository.create_corporate_action(
            reference=reference,
            instrument=prepared["instrument"],
            action_type=prepared["action_type"],
            ratio=prepared["ratio"],
            cash_rate=prepared["cash_rate"],
            record_date=prepared["record_date"],
            actor_id=actor.user_id,
        )

    def list_corporate_actions(self, actor: Actor, status: Optional[str] = None, limit: int = 100) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.list_corporate_actions(status=status, limit=limit)

    def get_corporate_action(self, actor: Actor, ca_id: int) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.get_corporate_action(ca_id)

    def entitlements(self, actor: Actor, ca_id: int) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.entitlements_for_ca(ca_id)

    def corporate_action_timeline(self, actor: Actor, ca_id: int) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        self.repository.get_corporate_action(ca_id)
        return self.repository.subject_timeline("corporate_action", ca_id)

    def _freeze(self, ca: Dict[str, Any]) -> Dict[str, Any]:
        movements = self.repository.movements_for_instrument(ca["instrument"])
        instructions = self.repository.list_records_by_instrument(ca["instrument"])
        return self.rules.freeze_eligible(ca, movements, instructions)

    def calculate_entitlements(self, actor: Actor, ca_id: int, expected_version: int) -> Dict[str, Any]:
        """按登记时点冻结合格数量并重算草稿权益（未定稿可反复重算）。"""
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        if not self.rules.role_can_manage_corporate_actions(actor.role):
            raise PermissionDenied("角色无权维护公司行动")
        ca = self.repository.get_corporate_action(ca_id)
        if ca["status"] != "open":
            raise Conflict("公司行动已定稿，不能重新计算")
        freeze = self._freeze(ca)
        rows, summary = self.rules.build_entitlements(ca, freeze)
        updated = self.repository.replace_draft_entitlements(ca_id, int(expected_version), rows, summary, actor.user_id)
        return {"ca": updated, "entitlements": self.repository.entitlements_for_ca(ca_id)}

    def finalize_corporate_action(self, actor: Actor, ca_id: int, expected_version: int, idempotency_key: Optional[str] = None) -> Dict[str, Any]:
        """定稿发放权益。

        - 同证券并发提交：乐观版本锁只放行一位，后到者 409；
        - 写入失败后用相同 idempotency_key 重试：沿用原指令冻结结果，不重复发放；
        - 登记时点后完成的交收只进 late 清单，已定稿权益不倒改。
        """
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        if not self.rules.role_can_manage_corporate_actions(actor.role):
            raise PermissionDenied("角色无权维护公司行动")
        if idempotency_key is not None:
            idempotency_key = text({"idempotency_key": idempotency_key}, "idempotency_key")
        ca = self.repository.get_corporate_action(ca_id)
        if ca["status"] == "finalized" and ca["version"] == int(expected_version):
            return {"ca": ca, "entitlements": self.repository.entitlements_for_ca(ca_id), "note": "公司行动已定稿，沿用已发权益"}
        freeze = self._freeze(ca)
        rows, frozen_summary = self.rules.build_entitlements(ca, freeze)
        response = {
            "ca": {"id": ca_id, "reference": ca["reference"], "instrument": ca["instrument"], "action_type": ca["action_type"], "status": "finalized", "version": int(expected_version) + 1},
            "entitlements": rows,
            "pending": freeze["pending"],
            "late": freeze["late"],
        }
        outcome = self.repository.finalize_corporate_action(
            ca_id=ca_id,
            expected_version=int(expected_version),
            rows=rows,
            frozen_summary=frozen_summary,
            actor_id=actor.user_id,
            idempotency_key=idempotency_key,
            response=response,
        )
        return outcome["response"]
