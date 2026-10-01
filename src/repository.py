"""SQLite 表结构与事务访问。

对象：
- records：结算指令（原状态机），交收完成后驱动持仓变动。
- holdings / holding_movements：持仓及其带生效时间（登记时点）的变动流水。
- corporate_actions / entitlements：公司行动与按登记时点冻结的权益。
- idempotency：写入失败后安全重试，保证权益不重复发放。
"""
import json
import sqlite3
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from .domain import Conflict, NotFound, ValidationError


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Repository:
    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path, timeout=15)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 15000")
        return connection

    def _init_schema(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS records (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    reference TEXT NOT NULL UNIQUE,
                    state TEXT NOT NULL,
                    version INTEGER NOT NULL DEFAULT 1,
                    payload TEXT NOT NULL,
                    created_by TEXT NOT NULL,
                    updated_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS holdings (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    account TEXT NOT NULL,
                    instrument TEXT NOT NULL,
                    quantity INTEGER NOT NULL,
                    recorded_at TEXT,
                    version INTEGER NOT NULL DEFAULT 1,
                    created_by TEXT NOT NULL,
                    updated_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(account, instrument)
                );
                CREATE TABLE IF NOT EXISTS holding_movements (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    holding_id INTEGER NOT NULL REFERENCES holdings(id) ON DELETE CASCADE,
                    record_id INTEGER REFERENCES records(id),
                    kind TEXT NOT NULL,
                    delta INTEGER NOT NULL,
                    effective_at TEXT,
                    reason TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS corporate_actions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    reference TEXT NOT NULL UNIQUE,
                    instrument TEXT NOT NULL,
                    action_type TEXT NOT NULL,
                    ratio REAL NOT NULL,
                    cash_rate REAL NOT NULL DEFAULT 0,
                    record_date TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'open',
                    version INTEGER NOT NULL DEFAULT 1,
                    summary TEXT NOT NULL DEFAULT '{}',
                    idempotency_key TEXT UNIQUE,
                    created_by TEXT NOT NULL,
                    updated_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS entitlements (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ca_id INTEGER NOT NULL REFERENCES corporate_actions(id) ON DELETE CASCADE,
                    account TEXT NOT NULL,
                    instrument TEXT NOT NULL,
                    ent_type TEXT NOT NULL,
                    quantity INTEGER NOT NULL DEFAULT 0,
                    cash_amount REAL NOT NULL DEFAULT 0,
                    status TEXT NOT NULL,
                    basis TEXT NOT NULL DEFAULT '[]',
                    created_at TEXT NOT NULL,
                    issued_at TEXT,
                    UNIQUE(ca_id, account)
                );
                CREATE TABLE IF NOT EXISTS idempotency (
                    key TEXT PRIMARY KEY,
                    subject_kind TEXT NOT NULL,
                    subject_id INTEGER NOT NULL,
                    response TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS audit_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    record_id INTEGER REFERENCES records(id) ON DELETE CASCADE,
                    subject_kind TEXT NOT NULL DEFAULT 'record',
                    subject_id INTEGER,
                    action TEXT NOT NULL,
                    actor_id TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    details TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_records_state ON records(state);
                CREATE INDEX IF NOT EXISTS idx_records_instrument ON records(json_extract(payload, '$.instrument'));
                CREATE INDEX IF NOT EXISTS idx_holdings_instrument ON holdings(instrument);
                CREATE INDEX IF NOT EXISTS idx_movements_holding ON holding_movements(holding_id, id);
                CREATE INDEX IF NOT EXISTS idx_ca_instrument_status ON corporate_actions(instrument, status);
                CREATE INDEX IF NOT EXISTS idx_entitlements_ca ON entitlements(ca_id);
                CREATE INDEX IF NOT EXISTS idx_audit_subject ON audit_events(subject_kind, subject_id, id);
                CREATE INDEX IF NOT EXISTS idx_audit_record ON audit_events(record_id, id);
                """
            )

    # ---------- 序列化辅助 ----------
    @staticmethod
    def _row(row: sqlite3.Row) -> Dict[str, Any]:
        item = dict(row)
        item["payload"] = json.loads(item["payload"])
        return item

    @staticmethod
    def _ca_row(row: sqlite3.Row) -> Dict[str, Any]:
        item = dict(row)
        item["summary"] = json.loads(item["summary"] or "{}")
        return item

    @staticmethod
    def _ent_row(row: sqlite3.Row) -> Dict[str, Any]:
        item = dict(row)
        item["basis"] = json.loads(item["basis"] or "[]")
        return item

    def _add_event(self, connection: sqlite3.Connection, subject_kind: str, subject_id: int, actor_id: str, action: str, version: int, details: Dict[str, Any], record_id: Optional[int] = None, now: str = None) -> None:
        connection.execute(
            "INSERT INTO audit_events(record_id,subject_kind,subject_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?,?,?)",
            (record_id if subject_kind == "record" else None, subject_kind, subject_id, action, actor_id, version, json.dumps(details, ensure_ascii=False, sort_keys=True), now or _now()),
        )

    # ---------- 结算指令 ----------
    def create(self, reference: str, state: str, payload: Dict[str, Any], actor_id: str) -> Dict[str, Any]:
        now = _now()
        try:
            with self._connect() as connection:
                cursor = connection.execute(
                    "INSERT INTO records(reference,state,version,payload,created_by,updated_by,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
                    (reference, state, 1, json.dumps(payload, ensure_ascii=False, sort_keys=True), actor_id, actor_id, now, now),
                )
                record_id = int(cursor.lastrowid)
                self._add_event(connection, "record", record_id, actor_id, "created", 1, {"state": state}, record_id=record_id, now=now)
                row = connection.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
        except sqlite3.IntegrityError as exc:
            raise Conflict("reference已存在") from exc
        return self._row(row)

    def get(self, record_id: int) -> Dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
        if row is None:
            raise NotFound("记录不存在")
        return self._row(row)

    def list_records(self, state: Optional[str] = None, limit: int = 100) -> List[Dict[str, Any]]:
        limit = max(1, min(int(limit), 500))
        with self._connect() as connection:
            if state:
                rows = connection.execute("SELECT * FROM records WHERE state=? ORDER BY id DESC LIMIT ?", (state, limit)).fetchall()
            else:
                rows = connection.execute("SELECT * FROM records ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [self._row(row) for row in rows]

    def list_records_by_instrument(self, instrument: str, limit: int = 500) -> List[Dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM records WHERE json_extract(payload,'$.instrument')=? ORDER BY id LIMIT ?",
                (instrument, max(1, min(int(limit), 1000))),
            ).fetchall()
        return [self._row(row) for row in rows]

    def mutate(self, record_id: int, expected_version: int, state: str, payload: Dict[str, Any], actor_id: str, action: str, details: Dict[str, Any]) -> Dict[str, Any]:
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT version FROM records WHERE id=?", (record_id,)).fetchone()
            if row is None:
                connection.rollback()
                raise NotFound("记录不存在")
            if int(row["version"]) != int(expected_version):
                connection.rollback()
                raise Conflict("版本冲突，请刷新后重试")
            version = int(expected_version) + 1
            connection.execute(
                "UPDATE records SET state=?,version=?,payload=?,updated_by=?,updated_at=? WHERE id=?",
                (state, version, json.dumps(payload, ensure_ascii=False, sort_keys=True), actor_id, now, record_id),
            )
            self._add_event(connection, "record", record_id, actor_id, action, version, details, record_id=record_id, now=now)
            result = connection.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
            connection.commit()
        return self._row(result)

    def settle_instruction(self, record: Dict[str, Any], expected_version: int, payload: Dict[str, Any], actor_id: str, details: Dict[str, Any], legs: List[tuple], settled_at: str) -> Dict[str, Any]:
        """交收原子操作：指令定稿 -> 双腿过户过账 -> 失效未定稿权益，全部在一个事务内。"""
        record_id = int(record["id"])
        instrument = str(payload["instrument"])
        now = _now()
        posted = []
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT version FROM records WHERE id=?", (record_id,)).fetchone()
            if row is None:
                connection.rollback()
                raise NotFound("记录不存在")
            if int(row["version"]) != int(expected_version):
                connection.rollback()
                raise Conflict("版本冲突，请刷新后重试")
            version = int(expected_version) + 1
            connection.execute(
                "UPDATE records SET state='settled',version=?,payload=?,updated_by=?,updated_at=? WHERE id=?",
                (version, json.dumps(payload, ensure_ascii=False, sort_keys=True), actor_id, now, record_id),
            )
            self._add_event(connection, "record", record_id, actor_id, "settle", version, details, record_id=record_id, now=now)

            for account, delta in legs:
                holding = connection.execute("SELECT * FROM holdings WHERE account=? AND instrument=?", (account, instrument)).fetchone()
                if holding is None:
                    if delta < 0:
                        connection.rollback()
                        raise ValidationError("账户%s持仓不足，无法过户" % account)
                    cursor = connection.execute(
                        "INSERT INTO holdings(account,instrument,quantity,recorded_at,version,created_by,updated_by,created_at,updated_at) VALUES(?,?,?,?,1,?,?,?,?)",
                        (account, instrument, delta, settled_at, actor_id, actor_id, now, now),
                    )
                    holding_id = int(cursor.lastrowid)
                    holding_version = 1
                else:
                    holding_id = int(holding["id"])
                    new_quantity = int(holding["quantity"]) + delta
                    if new_quantity < 0:
                        connection.rollback()
                        raise ValidationError("账户%s交收后持仓数量不能为负" % account)
                    connection.execute(
                        "UPDATE holdings SET quantity=?,updated_by=?,updated_at=? WHERE id=?",
                        (new_quantity, actor_id, now, holding_id),
                    )
                    holding_version = int(holding["version"]) + 1
                kind = "in" if delta > 0 else "out"
                connection.execute(
                    "INSERT INTO holding_movements(holding_id,record_id,kind,delta,effective_at,reason,created_at) VALUES(?,?,?,?,?,?,?)",
                    (holding_id, record_id, kind, delta, settled_at, "交收过户", now),
                )
                self._add_event(connection, "holding", holding_id, actor_id, "settlement_posted", holding_version, {"record_id": record_id, "delta": delta, "effective_at": settled_at}, now=now)
                posted.append({"account": account, "delta": delta})

            self._invalidate_open_drafts(connection, instrument, "持仓因交收发生变化（指令%s）" % record["reference"], actor_id, now)
            result = connection.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
            connection.commit()
        return self._row(result)

    # ---------- 持仓 ----------
    def create_holding(self, account: str, instrument: str, quantity: int, recorded_at: Optional[str], actor_id: str) -> Dict[str, Any]:
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute("SELECT id FROM holdings WHERE account=? AND instrument=?", (account, instrument)).fetchone()
            if existing is not None:
                connection.rollback()
                raise Conflict("该账户在此证券上已有持仓")
            cursor = connection.execute(
                "INSERT INTO holdings(account,instrument,quantity,recorded_at,version,created_by,updated_by,created_at,updated_at) VALUES(?,?,?,?,1,?,?,?,?)",
                (account, instrument, quantity, recorded_at, actor_id, actor_id, now, now),
            )
            holding_id = int(cursor.lastrowid)
            connection.execute(
                "INSERT INTO holding_movements(holding_id,record_id,kind,delta,effective_at,reason,created_at) VALUES(?,?,?,?,?,?,?)",
                (holding_id, None, "opening", quantity, recorded_at, "期初持仓", now),
            )
            self._add_event(connection, "holding", holding_id, actor_id, "opening_created", 1, {"quantity": quantity, "recorded_at": recorded_at, "legacy": recorded_at is None}, now=now)
            self._invalidate_open_drafts(connection, instrument, "新增期初持仓", actor_id, now)
            row = connection.execute("SELECT * FROM holdings WHERE id=?", (holding_id,)).fetchone()
            connection.commit()
        return dict(row)

    def get_holding(self, holding_id: int) -> Dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM holdings WHERE id=?", (holding_id,)).fetchone()
        if row is None:
            raise NotFound("持仓不存在")
        return dict(row)

    def list_holdings(self, instrument: Optional[str] = None, account: Optional[str] = None, limit: int = 100) -> List[Dict[str, Any]]:
        clauses, params = [], []
        if instrument:
            clauses.append("instrument=?")
            params.append(instrument)
        if account:
            clauses.append("account=?")
            params.append(account)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        params.append(max(1, min(int(limit), 500)))
        with self._connect() as connection:
            rows = connection.execute("SELECT * FROM holdings%s ORDER BY id DESC LIMIT ?" % where, tuple(params)).fetchall()
        return [dict(row) for row in rows]

    def movements_for_instrument(self, instrument: str) -> List[Dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT m.id, m.holding_id, m.record_id, m.kind, m.delta, m.effective_at, m.reason,
                       h.account, h.instrument
                FROM holding_movements m JOIN holdings h ON h.id = m.holding_id
                WHERE h.instrument=? ORDER BY m.id
                """,
                (instrument,),
            ).fetchall()
        return [dict(row) for row in rows]

    def adjust_holding(self, holding_id: int, expected_version: int, delta: int, reason: str, actor_id: str) -> Dict[str, Any]:
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            holding = connection.execute("SELECT * FROM holdings WHERE id=?", (holding_id,)).fetchone()
            if holding is None:
                connection.rollback()
                raise NotFound("持仓不存在")
            if int(holding["version"]) != int(expected_version):
                connection.rollback()
                raise Conflict("版本冲突，请刷新后重试")
            new_quantity = int(holding["quantity"]) + delta
            if new_quantity < 0:
                connection.rollback()
                raise ValidationError("调整后持仓数量不能为负")
            version = int(expected_version) + 1
            connection.execute(
                "UPDATE holdings SET quantity=?,version=?,updated_by=?,updated_at=? WHERE id=?",
                (new_quantity, version, actor_id, now, holding_id),
            )
            connection.execute(
                "INSERT INTO holding_movements(holding_id,record_id,kind,delta,effective_at,reason,created_at) VALUES(?,?,?,?,?,?,?)",
                (holding_id, None, "adjustment", delta, holding["recorded_at"], reason or "人工调整", now),
            )
            self._add_event(connection, "holding", holding_id, actor_id, "adjusted", version, {"delta": delta, "reason": reason, "new_quantity": new_quantity}, now=now)
            self._invalidate_open_drafts(connection, holding["instrument"], "持仓人工调整", actor_id, now)
            row = connection.execute("SELECT * FROM holdings WHERE id=?", (holding_id,)).fetchone()
            connection.commit()
        return dict(row)

    # ---------- 公司行动与权益 ----------
    def create_corporate_action(self, reference: str, instrument: str, action_type: str, ratio: float, cash_rate: float, record_date: str, actor_id: str) -> Dict[str, Any]:
        """建立公司行动；同事务把旧持仓缺失的登记时点补齐为本次（首次）登记时点。"""
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                cursor = connection.execute(
                    "INSERT INTO corporate_actions(reference,instrument,action_type,ratio,cash_rate,record_date,status,version,summary,created_by,updated_by,created_at,updated_at) VALUES(?,?,?,?,?,?,'open',1,'{}',?,?,?,?)",
                    (reference, instrument, action_type, ratio, cash_rate, record_date, actor_id, actor_id, now, now),
                )
            except sqlite3.IntegrityError as exc:
                connection.rollback()
                raise Conflict("reference已存在") from exc
            ca_id = int(cursor.lastrowid)

            legacy = connection.execute(
                "SELECT id, account, quantity FROM holdings WHERE instrument=? AND recorded_at IS NULL ORDER BY id",
                (instrument,),
            ).fetchall()
            backfilled = []
            for item in legacy:
                connection.execute("UPDATE holdings SET recorded_at=?,updated_by=?,updated_at=? WHERE id=?", (record_date, actor_id, now, item["id"]))
                connection.execute(
                    "UPDATE holding_movements SET effective_at=? WHERE holding_id=? AND effective_at IS NULL",
                    (record_date, item["id"]),
                )
                self._add_event(connection, "holding", int(item["id"]), actor_id, "record_point_backfilled", 1, {"record_date": record_date, "ca_id": ca_id, "quantity": int(item["quantity"])}, now=now)
                backfilled.append({"holding_id": int(item["id"]), "account": item["account"], "quantity": int(item["quantity"])})

            self._add_event(connection, "corporate_action", ca_id, actor_id, "created", 1, {"instrument": instrument, "action_type": action_type, "record_date": record_date, "backfilled": backfilled}, now=now)
            row = connection.execute("SELECT * FROM corporate_actions WHERE id=?", (ca_id,)).fetchone()
            connection.commit()
        return self._ca_row(row)

    def get_corporate_action(self, ca_id: int) -> Dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM corporate_actions WHERE id=?", (ca_id,)).fetchone()
        if row is None:
            raise NotFound("公司行动不存在")
        return self._ca_row(row)

    def list_corporate_actions(self, status: Optional[str] = None, limit: int = 100) -> List[Dict[str, Any]]:
        limit = max(1, min(int(limit), 500))
        with self._connect() as connection:
            if status:
                rows = connection.execute("SELECT * FROM corporate_actions WHERE status=? ORDER BY id DESC LIMIT ?", (status, limit)).fetchall()
            else:
                rows = connection.execute("SELECT * FROM corporate_actions ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [self._ca_row(row) for row in rows]

    def replace_draft_entitlements(self, ca_id: int, expected_version: int, rows: List[Dict[str, Any]], summary: Dict[str, Any], actor_id: str) -> Dict[str, Any]:
        """按登记时点重算：乐观锁 + 整批替换草稿权益。"""
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            ca = connection.execute("SELECT version,status FROM corporate_actions WHERE id=?", (ca_id,)).fetchone()
            if ca is None:
                connection.rollback()
                raise NotFound("公司行动不存在")
            if ca["status"] != "open":
                connection.rollback()
                raise Conflict("公司行动已定稿，不能重新计算")
            if int(ca["version"]) != int(expected_version):
                connection.rollback()
                raise Conflict("版本冲突，请刷新后重试")
            version = int(expected_version) + 1
            connection.execute("DELETE FROM entitlements WHERE ca_id=? AND status='draft'", (ca_id,))
            for item in rows:
                connection.execute(
                    "INSERT INTO entitlements(ca_id,account,instrument,ent_type,quantity,cash_amount,status,basis,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                    (ca_id, item["account"], item["instrument"], item["ent_type"], int(item["quantity"]), float(item["cash_amount"]), "draft", json.dumps(item.get("basis", []), ensure_ascii=False), now),
                )
            connection.execute(
                "UPDATE corporate_actions SET version=?,summary=?,updated_by=?,updated_at=? WHERE id=?",
                (version, json.dumps(summary, ensure_ascii=False, sort_keys=True), actor_id, now, ca_id),
            )
            self._add_event(connection, "corporate_action", ca_id, actor_id, "calculate", version, {"accounts": list(summary.get("accounts", {}).keys()), "pending": len(summary.get("pending", [])), "late": len(summary.get("late", []))}, now=now)
            row = connection.execute("SELECT * FROM corporate_actions WHERE id=?", (ca_id,)).fetchone()
            connection.commit()
        return self._ca_row(row)

    def finalize_corporate_action(self, ca_id: int, expected_version: int, rows: List[Dict[str, Any]], frozen_summary: Dict[str, Any], actor_id: str, idempotency_key: Optional[str], response: Dict[str, Any]) -> Dict[str, Any]:
        """定稿发放：同一公司行动只放行一次；幂等键保证失败重试不重复发放。

        返回 {"replayed": False, "response": {...}} 或命中幂等键时的已存响应。
        """
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if idempotency_key:
                hit = connection.execute("SELECT response FROM idempotency WHERE key=?", (idempotency_key,)).fetchone()
                if hit is not None:
                    connection.rollback()
                    return {"replayed": True, "response": json.loads(hit["response"])}

            ca = connection.execute("SELECT * FROM corporate_actions WHERE id=?", (ca_id,)).fetchone()
            if ca is None:
                connection.rollback()
                raise NotFound("公司行动不存在")
            if int(ca["version"]) != int(expected_version):
                connection.rollback()
                raise Conflict("版本冲突，请刷新后重试")
            if ca["status"] == "finalized":
                # 版本校验已通过但状态已定稿：原样回放已发权益（不重复发放）
                connection.rollback()
                issued_rows = connection.execute("SELECT * FROM entitlements WHERE ca_id=? ORDER BY account", (ca_id,)).fetchall()
                return {"replayed": True, "response": {"ca": self._ca_row(ca), "entitlements": [self._ent_row(row) for row in issued_rows], "note": "公司行动已定稿，沿用已发权益"}}
            version = int(expected_version) + 1

            if idempotency_key:
                try:
                    connection.execute(
                        "INSERT INTO idempotency(key,subject_kind,subject_id,response,created_at) VALUES(?,?,?,?,?)",
                        (idempotency_key, "corporate_action", ca_id, json.dumps(response, ensure_ascii=False, sort_keys=True), now),
                    )
                except sqlite3.IntegrityError:
                    connection.rollback()
                    hit = self.get_idempotency(idempotency_key)
                    return {"replayed": True, "response": hit}

            connection.execute("DELETE FROM entitlements WHERE ca_id=? AND status='draft'", (ca_id,))
            for item in rows:
                connection.execute(
                    "INSERT INTO entitlements(ca_id,account,instrument,ent_type,quantity,cash_amount,status,basis,created_at,issued_at) VALUES(?,?,?,?,?,?, 'issued', ?, ?, ?)",
                    (ca_id, item["account"], item["instrument"], item["ent_type"], int(item["quantity"]), float(item["cash_amount"]), json.dumps(item.get("basis", []), ensure_ascii=False), now, now),
                )
            connection.execute(
                "UPDATE corporate_actions SET status='finalized',version=?,summary=?,idempotency_key=COALESCE(idempotency_key,?),updated_by=?,updated_at=? WHERE id=?",
                (version, json.dumps(frozen_summary, ensure_ascii=False, sort_keys=True), idempotency_key, actor_id, now, ca_id),
            )
            self._add_event(connection, "corporate_action", ca_id, actor_id, "finalize", version, {"accounts": list(frozen_summary.get("accounts", {}).keys()), "pending": frozen_summary.get("pending", []), "late": frozen_summary.get("late", []), "idempotency_key": idempotency_key}, now=now)
            issued_rows = connection.execute("SELECT * FROM entitlements WHERE ca_id=? ORDER BY account", (ca_id,)).fetchall()
            stored_entitlements = [self._ent_row(row) for row in issued_rows]
            response["entitlements"] = stored_entitlements
            # 幂等键缓存的响应与首次返回保持一致
            connection.execute(
                "UPDATE idempotency SET response=? WHERE key=?",
                (json.dumps(response, ensure_ascii=False, sort_keys=True), idempotency_key),
            )
            connection.commit()
        return {"replayed": False, "response": response}

    def get_idempotency(self, key: str) -> Dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute("SELECT response FROM idempotency WHERE key=?", (key,)).fetchone()
        if row is None:
            raise NotFound("幂等键不存在")
        return json.loads(row["response"])

    def entitlements_for_ca(self, ca_id: int) -> List[Dict[str, Any]]:
        self.get_corporate_action(ca_id)
        with self._connect() as connection:
            rows = connection.execute("SELECT * FROM entitlements WHERE ca_id=? ORDER BY account", (ca_id,)).fetchall()
        return [self._ent_row(row) for row in rows]

    def _invalidate_open_drafts(self, connection: sqlite3.Connection, instrument: str, reason: str, actor_id: str, now: str) -> int:
        """持仓数量变化后，同一证券所有未定稿权益立即失效：删草稿、版本抬升，强制重算。"""
        open_cas = connection.execute("SELECT id, version FROM corporate_actions WHERE instrument=? AND status='open'", (instrument,)).fetchall()
        for ca in open_cas:
            ca_id, old_version = int(ca["id"]), int(ca["version"])
            connection.execute("DELETE FROM entitlements WHERE ca_id=? AND status='draft'", (ca_id,))
            stale = {"phase": "stale", "reason": reason}
            connection.execute(
                "UPDATE corporate_actions SET version=?,summary=?,updated_by=?,updated_at=? WHERE id=?",
                (old_version + 1, json.dumps(stale, ensure_ascii=False, sort_keys=True), actor_id, now, ca_id),
            )
            self._add_event(connection, "corporate_action", ca_id, actor_id, "entitlements_invalidated", old_version + 1, stale, now=now)
        return len(open_cas)

    # ---------- 审计 ----------
    def add_audit(self, record_id: int, actor_id: str, action: str, details: Dict[str, Any]) -> None:
        with self._connect() as connection:
            row = connection.execute("SELECT version FROM records WHERE id=?", (record_id,)).fetchone()
            if row is None:
                raise NotFound("记录不存在")
            self._add_event(connection, "record", record_id, actor_id, action, int(row["version"]), details)
            connection.commit()

    def audit_timeline(self, record_id: int) -> List[Dict[str, Any]]:
        self.get(record_id)
        with self._connect() as connection:
            rows = connection.execute("SELECT * FROM audit_events WHERE subject_kind='record' AND subject_id=? ORDER BY id", (record_id,)).fetchall()
        return [self._event_row(row) for row in rows]

    def subject_timeline(self, subject_kind: str, subject_id: int) -> List[Dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM audit_events WHERE subject_kind=? AND subject_id=? ORDER BY id",
                (subject_kind, subject_id),
            ).fetchall()
        return [self._event_row(row) for row in rows]

    @staticmethod
    def _event_row(row: sqlite3.Row) -> Dict[str, Any]:
        item = dict(row)
        item["details"] = json.loads(item["details"])
        return item

    def stats(self) -> Dict[str, int]:
        with self._connect() as connection:
            rows = connection.execute("SELECT state, COUNT(*) AS total FROM records GROUP BY state").fetchall()
        return {str(row["state"]): int(row["total"]) for row in rows}

    def health(self) -> bool:
        try:
            with self._connect() as connection:
                connection.execute("SELECT 1").fetchone()
            return True
        except sqlite3.Error:
            return False
