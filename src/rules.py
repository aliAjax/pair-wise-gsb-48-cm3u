"""证券结算与企业行动处理领域规则与状态转换。"""
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, Optional, Tuple

from .domain import Conflict, ValidationError, choice, integer, number, optional_text, text


INITIAL_STATE = "captured"
CREATE_ROLES = {'trader'}
ACTION_ROLES = {
    'apply_corporate': {'corporate_actions'},
    'approve': {'settlement_officer'},
    'settle': {'settlement_officer'},
    'fail': {'settlement_officer'},
    'reverse': {'corporate_actions', 'settlement_officer'},
    'create_position': {'settlement_officer'},
    'adjust_position': {'settlement_officer'},
    'create_corporate_action': {'corporate_actions'},
    'freeze_corporate_action': {'corporate_actions'},
    'issue_corporate_action': {'corporate_actions'},
}
TRANSITIONS = {
    'apply_corporate': {'captured': 'adjusted'},
    'approve': {'captured': 'approved', 'adjusted': 'approved'},
    'settle': {'approved': 'settled'},
    'fail': {'approved': 'failed'},
    'reverse': {'settled': 'reversed', 'failed': 'reversed'},
}
CORPORATE_ACTION_TYPES = ["split", "dividend", "merger"]


def parse_datetime(value: Any, field: str) -> str:
    """解析为UTC ISO字符串；空值由调用方处理。"""
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str) and value.strip():
        try:
            parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValidationError("%s必须是ISO时间" % field) from exc
    else:
        raise ValidationError("%s必须是ISO时间" % field)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat()


def optional_datetime(data: Dict[str, Any], key: str, default: Optional[str] = None) -> Optional[str]:
    value = data.get(key, default)
    if value is None or value == "":
        return default
    return parse_datetime(value, key)


class DomainRules:
    INITIAL_STATE = INITIAL_STATE

    def known_role(self, role: str) -> bool:
        all_roles = set(CREATE_ROLES)
        for roles in ACTION_ROLES.values():
            all_roles.update(roles)
        return role == "admin" or role in all_roles

    def role_can_create(self, role: str) -> bool:
        return role == "admin" or role in CREATE_ROLES

    def role_can_action(self, role: str, action: str) -> bool:
        return role == "admin" or role in ACTION_ROLES.get(action, set())

    def validate_create(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        p = dict(payload)
        text(p, "instrument")
        choice(p, "side", ["buy", "sell"])
        integer(p, "quantity", 1)
        number(p, "price", 0.01)
        number(p, "fees", 0)
        choice(p, "currency", ["CNY", "USD", "HKD"])
        integer(p, "settlement_day", 0)
        choice(p, "corporate_action", ["none", "split", "dividend", "merger"])
        number(p, "action_ratio", 0.01)
        p["account_id"] = optional_text(p, "account_id", "DEFAULT")
        p["trade_at"] = optional_datetime(p, "trade_at")
        p["settlement_due_at"] = optional_datetime(p, "settlement_due_at")
        return p

    def prepare_create(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        p = self.validate_create(payload)
        gross = float(p["quantity"]) * float(p["price"])
        fee = float(p["fees"])
        p["gross_amount"] = round(gross, 2)
        p["net_amount"] = round(gross + fee if p["side"] == "buy" else gross - fee, 2)
        p["adjusted_quantity"] = p["quantity"]
        p["adjusted_price"] = p["price"]
        if not p["settlement_due_at"] and p["trade_at"]:
            trade_at = datetime.fromisoformat(p["trade_at"])
            p["settlement_due_at"] = (trade_at + timedelta(days=int(p["settlement_day"]))).isoformat()
        if p["corporate_action"] == "split":
            p["adjusted_quantity"] = int(float(p["quantity"]) * float(p["action_ratio"]))
            p["adjusted_price"] = round(float(p["price"]) / float(p["action_ratio"]), 4)
        elif p["corporate_action"] == "dividend":
            p["cash_entitlement"] = round(float(p["quantity"]) * float(p["action_ratio"]), 2)
        return p

    def prepare_corporate_action(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        p = dict(payload or {})
        result = {
            "instrument": text(p, "instrument"),
            "action_type": choice(p, "action_type", CORPORATE_ACTION_TYPES),
            "ratio": number(p, "ratio", 0),
            "record_time": parse_datetime(p.get("record_time"), "record_time"),
            "currency": p.get("currency", "CNY") if p.get("currency") is not None else "CNY",
        }
        choice(result, "currency", ["CNY", "USD", "HKD"])
        return result

    @staticmethod
    def entitlement_for_quantity(quantity: int, action_type: str, ratio: float, currency: str) -> Dict[str, Any]:
        quantity = int(quantity)
        entitlement = {
            "eligible_quantity": quantity,
            "entitlement_quantity": 0,
            "cash_amount": 0.0,
            "currency": currency,
        }
        if action_type == "dividend":
            entitlement["cash_amount"] = round(quantity * float(ratio), 2)
        else:
            entitlement["entitlement_quantity"] = int(quantity * float(ratio))
        return entitlement

    def check_create_conflicts(self, payload: Dict[str, Any], existing: Iterable[Dict[str, Any]]) -> None:
        for item in existing:
            if item["state"] not in {"settled", "reversed"} and item["payload"].get("instrument") == payload.get("instrument") and item["payload"].get("settlement_day") == payload.get("settlement_day"):
                if item["payload"].get("side") == payload.get("side") and item["payload"].get("quantity") == payload.get("quantity") and item["payload"].get("price") == payload.get("price"):
                    raise Conflict("疑似重复结算指令")

    def require_transition(self, record: Dict[str, Any], action: str) -> str:
        allowed = TRANSITIONS.get(action, {}).get(record["state"])
        if allowed is None:
            raise Conflict("当前状态不允许执行%s" % action)
        return allowed

    def apply_action(self, record: Dict[str, Any], action: str, data: Dict[str, Any]) -> Tuple[str, Dict[str, Any], str]:
        new_state = self.require_transition(record, action)
        data = dict(data or {})
        p = dict(record["payload"])
        changes: Dict[str, Any] = {}
        summary = ""
        if action == "apply_corporate":
            if p["corporate_action"] == "none":
                raise ValidationError("没有待处理的公司行动")
            changes["corporate_applied"] = True
            changes["effective_quantity"] = p["adjusted_quantity"]
            changes["effective_price"] = p["adjusted_price"]
            summary = "公司行动已应用"
        elif action == "approve":
            changes["approved_amount"] = p["net_amount"]
            summary = "结算指令复核通过"
        elif action == "settle":
            delivered = integer(data, "delivered_quantity", 0)
            paid = number(data, "cash_paid", 0)
            required_quantity = int(p.get("effective_quantity", p["quantity"]))
            if delivered != required_quantity:
                raise ValidationError("交收证券数量不匹配")
            if paid < float(p["net_amount"]):
                raise ValidationError("交收资金不足")
            changes["delivered_quantity"] = delivered
            changes["cash_paid"] = paid
            changes["settled_at"] = optional_datetime(data, "settled_at", datetime.now(timezone.utc).isoformat())
            request_id = optional_text(data, "request_id")
            if request_id:
                changes["settlement_request_id"] = request_id
            summary = "交收完成"
        elif action == "fail":
            changes["fail_reason"] = text(data, "fail_reason")
            summary = "交收失败"
        elif action == "reverse":
            changes["reverse_reason"] = text(data, "reverse_reason")
            summary = "交收冲正"
        p.update(changes)
        return new_state, p, summary or ("已执行%s" % action)
