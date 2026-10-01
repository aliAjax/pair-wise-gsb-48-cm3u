"""证券结算与企业行动处理领域规则与状态转换。

权益冻结的核心口径：
- 每笔持仓变动（期初开仓 / 交收过户 / 人工调整）都带生效时间 effective_at；
- 仅 effective_at <= 公司行动登记时点 的变动计入合格数量；
- 登记时点仍未交收的指令进 pending（待核，权益不转移）；
- 时点之后才完成交收的指令进 late（权益已定稿，不得倒改）。
"""
from datetime import datetime
from typing import Any, Dict, Iterable, List, Tuple

from .domain import Actor, Conflict, ValidationError, boolean, choice, integer, number, text, text_list


INITIAL_STATE = "captured"
CREATE_ROLES = {'trader'}
ACTION_ROLES = {'apply_corporate': {'corporate_actions'}, 'approve': {'settlement_officer'}, 'settle': {'settlement_officer'}, 'fail': {'settlement_officer'}, 'reverse': {'corporate_actions', 'settlement_officer'}}
HOLDING_ROLES = {'settlement_officer'}
CORPORATE_ACTION_ROLES = {'corporate_actions'}
TRANSITIONS = {'apply_corporate': {'captured': 'adjusted'}, 'approve': {'captured': 'approved', 'adjusted': 'approved'}, 'settle': {'approved': 'settled'}, 'fail': {'approved': 'failed'}, 'reverse': {'settled': 'reversed', 'failed': 'reversed'}}
CA_TYPES = ["split", "dividend", "merger"]
# 未定稿（未交收完成）的指令状态：登记时点落在这些状态上即为待核
OPEN_INSTRUCTION_STATES = {"captured", "adjusted", "approved", "failed", "reversed"}


def parse_iso(value: str, key: str = "时间") -> str:
    """解析 ISO 8601 时间并归一化为可字典序比较的 UTC 字符串。"""
    if not isinstance(value, str) or not value.strip():
        raise ValidationError("%s必须是ISO 8601时间" % key)
    raw = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError as exc:
        raise ValidationError("%s必须是ISO 8601时间" % key) from exc
    return _normalize(parsed)


def _normalize(parsed: datetime) -> str:
    from datetime import timezone
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat()


def now_iso() -> str:
    from datetime import timezone
    return datetime.now(timezone.utc).isoformat()


class DomainRules:
    INITIAL_STATE = INITIAL_STATE
    OPEN_INSTRUCTION_STATES = OPEN_INSTRUCTION_STATES

    def known_role(self, role: str) -> bool:
        all_roles = set(CREATE_ROLES)
        for roles in ACTION_ROLES.values():
            all_roles.update(roles)
        all_roles.update(HOLDING_ROLES)
        all_roles.update(CORPORATE_ACTION_ROLES)
        return role == "admin" or role in all_roles

    def role_can_create(self, role: str) -> bool:
        return role == "admin" or role in CREATE_ROLES

    def role_can_action(self, role: str, action: str) -> bool:
        return role == "admin" or role in ACTION_ROLES.get(action, set())

    def role_can_manage_holdings(self, role: str) -> bool:
        return role == "admin" or role in HOLDING_ROLES

    def role_can_manage_corporate_actions(self, role: str) -> bool:
        return role == "admin" or role in CORPORATE_ACTION_ROLES

    # ---------- 结算指令 ----------
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
        account = p.get("account")
        if account is not None:
            if not isinstance(account, str) or not account.strip():
                raise ValidationError("account必须是文本")
            p["account"] = account.strip()
        for counterparty_key in ("buyer_account", "seller_account"):
            counterparty = p.get(counterparty_key)
            if counterparty is not None:
                if not isinstance(counterparty, str) or not counterparty.strip():
                    raise ValidationError("%s必须是文本" % counterparty_key)
                p[counterparty_key] = counterparty.strip()
        return p

    def prepare_create(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        p = self.validate_create(payload)
        gross = float(p["quantity"]) * float(p["price"])
        fee = float(p["fees"])
        p["gross_amount"] = round(gross, 2)
        p["net_amount"] = round(gross + fee if p["side"] == "buy" else gross - fee, 2)
        p["adjusted_quantity"] = p["quantity"]
        p["adjusted_price"] = p["price"]
        if p["corporate_action"] == "split":
            p["adjusted_quantity"] = int(float(p["quantity"]) * float(p["action_ratio"]))
            p["adjusted_price"] = round(float(p["price"]) / float(p["action_ratio"]), 4)
        elif p["corporate_action"] == "dividend":
            p["cash_entitlement"] = round(float(p["quantity"]) * float(p["action_ratio"]), 2)
        return p

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
            # 交收生效时间：默认当前时刻；允许显式传入以便按登记时点判定权益归属
            changes["settled_at"] = parse_iso(data["settled_at"], "settled_at") if data.get("settled_at") else now_iso()
            summary = "交收完成"
        elif action == "fail":
            changes["fail_reason"] = text(data, "fail_reason")
            summary = "交收失败"
        elif action == "reverse":
            changes["reverse_reason"] = text(data, "reverse_reason")
            summary = "交收冲正"
        p.update(changes)
        return new_state, p, summary or ("已执行%s" % action)

    @staticmethod
    def settlement_legs(payload: Dict[str, Any]) -> List[Tuple[str, int]]:
        """交收过户的本系统持仓腿：买方入、卖方出；对手方在系统外，不建账。"""
        delivered = int(payload.get("delivered_quantity", payload.get("effective_quantity", payload["quantity"])))
        if payload.get("side") == "buy":
            account = payload.get("buyer_account") or payload.get("account") or "MAIN"
            return [(str(account), delivered)]
        account = payload.get("seller_account") or payload.get("account") or "MAIN"
        return [(str(account), -delivered)]

    # ---------- 持仓 ----------
    def validate_holding(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        account = payload.get("account", "MAIN")
        if not isinstance(account, str) or not account.strip():
            raise ValidationError("account必须是文本")
        instrument = text(payload, "instrument")
        quantity = integer(payload, "quantity", 0)
        recorded_at = None
        if payload.get("recorded_at"):
            recorded_at = parse_iso(payload["recorded_at"], "recorded_at")
        return {"account": account.strip(), "instrument": instrument, "quantity": quantity, "recorded_at": recorded_at}

    def validate_adjustment(self, payload: Dict[str, Any]) -> Tuple[int, str]:
        delta = integer(payload, "delta")
        if delta == 0:
            raise ValidationError("delta不能为0")
        return delta, text(payload, "reason")

    # ---------- 公司行动 ----------
    def validate_corporate_action(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        instrument = text(payload, "instrument")
        action_type = choice(payload, "action_type", CA_TYPES)
        ratio = number(payload, "ratio", 0)
        cash_rate = number(payload, "cash_rate", 0) if "cash_rate" in payload else 0.0
        record_date = parse_iso(payload.get("record_date"), "record_date")
        if action_type != "dividend" and ratio <= 0:
            raise ValidationError("送股/合并类公司行动ratio必须大于0")
        if action_type == "dividend" and cash_rate <= 0:
            raise ValidationError("分红公司行动cash_rate必须大于0")
        return {"instrument": instrument, "action_type": action_type, "ratio": ratio, "cash_rate": cash_rate, "record_date": record_date}

    def freeze_eligible(self, ca: Dict[str, Any], movements: List[Dict[str, Any]], instructions: List[Dict[str, Any]]) -> Dict[str, Any]:
        """按登记时点冻结合格数量，并列出待核/迟办指令。"""
        record_date = ca["record_date"]
        instrument = ca["instrument"]
        eligible: Dict[str, int] = {}
        basis: Dict[str, List[Dict[str, Any]]] = {}
        for movement in movements:
            effective_at = movement.get("effective_at")
            if effective_at is None:
                # 旧持仓缺登记时点且未被补齐：不计入合格数量
                continue
            if effective_at <= record_date:
                account = movement["account"]
                eligible[account] = eligible.get(account, 0) + int(movement["delta"])
                basis.setdefault(account, []).append({
                    "movement_id": movement["id"],
                    "kind": movement["kind"],
                    "delta": int(movement["delta"]),
                    "effective_at": effective_at,
                })

        pending, late = [], []
        for record in instructions:
            payload = record.get("payload", {})
            if payload.get("instrument") != instrument:
                continue
            entry = {
                "reference": record.get("reference"),
                "record_id": record.get("id"),
                "side": payload.get("side"),
                "quantity": int(payload.get("effective_quantity", payload.get("quantity", 0))),
            }
            if record["state"] in OPEN_INSTRUCTION_STATES:
                # 登记时点前未完成交收：过户未确认，待核，权益不转移
                item = dict(entry)
                item["state"] = record["state"]
                pending.append(item)
            elif record["state"] == "settled":
                settled_at = payload.get("settled_at")
                if settled_at is None or settled_at > record_date:
                    # 登记时点之后才完成：不能倒改已发权益
                    item = dict(entry)
                    item["settled_at"] = settled_at
                    late.append(item)

        accounts: Dict[str, Dict[str, Any]] = {}
        for account, quantity in eligible.items():
            if quantity <= 0:
                continue
            accounts[account] = {"eligible": quantity, "movements": basis.get(account, [])}
        return {"record_date": record_date, "accounts": accounts, "pending": pending, "late": late}

    def build_entitlements(self, ca: Dict[str, Any], freeze: Dict[str, Any]) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
        rows: List[Dict[str, Any]] = []
        summary_accounts: Dict[str, Dict[str, Any]] = {}
        for account, info in freeze["accounts"].items():
            eligible = int(info["eligible"])
            basis = info["movements"]
            if ca["action_type"] == "dividend":
                row = {"account": account, "instrument": ca["instrument"], "ent_type": "cash", "quantity": 0, "cash_amount": round(eligible * float(ca["cash_rate"]), 2), "basis": basis}
            else:
                row = {"account": account, "instrument": ca["instrument"], "ent_type": "securities", "quantity": int(eligible * float(ca["ratio"])), "cash_amount": 0.0, "basis": basis}
            rows.append(row)
            summary_accounts[account] = {"eligible": eligible, "quantity": row["quantity"], "cash_amount": row["cash_amount"]}
        summary = {
            "phase": "calculated",
            "record_date": freeze["record_date"],
            "accounts": summary_accounts,
            "pending": freeze["pending"],
            "late": freeze["late"],
        }
        return rows, summary
