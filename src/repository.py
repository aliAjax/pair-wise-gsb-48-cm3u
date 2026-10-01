"""SQLite 表结构与事务访问。"""
import json
import sqlite3
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from .domain import Conflict, NotFound


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
                CREATE TABLE IF NOT EXISTS audit_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    record_id INTEGER NOT NULL REFERENCES records(id) ON DELETE CASCADE,
                    action TEXT NOT NULL,
                    actor_id TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    details TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS positions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    instrument TEXT NOT NULL,
                    account_id TEXT NOT NULL,
                    quantity INTEGER NOT NULL,
                    recorded_at TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(instrument, account_id)
                );
                CREATE TABLE IF NOT EXISTS position_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    instrument TEXT NOT NULL,
                    account_id TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    delta INTEGER NOT NULL,
                    recorded_at TEXT,
                    event_key TEXT NOT NULL UNIQUE,
                    request_id TEXT,
                    instruction_id INTEGER REFERENCES records(id),
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS corporate_actions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    reference TEXT NOT NULL UNIQUE,
                    instrument TEXT NOT NULL,
                    status TEXT NOT NULL CHECK(status IN ('draft','frozen','issued')),
                    version INTEGER NOT NULL DEFAULT 1,
                    payload TEXT NOT NULL,
                    freeze_request_id TEXT UNIQUE,
                    issue_request_id TEXT UNIQUE,
                    created_by TEXT NOT NULL,
                    updated_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    frozen_at TEXT,
                    issued_at TEXT
                );
                CREATE TABLE IF NOT EXISTS entitlements (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    corporate_action_id INTEGER NOT NULL REFERENCES corporate_actions(id) ON DELETE CASCADE,
                    account_id TEXT NOT NULL,
                    status TEXT NOT NULL CHECK(status IN ('provisional','frozen','issued','superseded')),
                    eligible_quantity INTEGER NOT NULL,
                    entitlement_quantity INTEGER NOT NULL DEFAULT 0,
                    cash_amount REAL NOT NULL DEFAULT 0,
                    currency TEXT NOT NULL,
                    version INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    frozen_at TEXT,
                    issued_at TEXT
                );
                CREATE TABLE IF NOT EXISTS instruction_reviews (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    corporate_action_id INTEGER NOT NULL REFERENCES corporate_actions(id) ON DELETE CASCADE,
                    instruction_id INTEGER NOT NULL REFERENCES records(id) ON DELETE CASCADE,
                    account_id TEXT NOT NULL,
                    side TEXT NOT NULL,
                    quantity INTEGER NOT NULL,
                    status TEXT NOT NULL CHECK(status IN ('pending','settled_after_record','failed_after_record','reviewed')),
                    reason TEXT NOT NULL,
                    due_at TEXT,
                    settled_at TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(corporate_action_id, instruction_id)
                );
                CREATE INDEX IF NOT EXISTS idx_records_state ON records(state);
                CREATE INDEX IF NOT EXISTS idx_audit_record ON audit_events(record_id, id);
                CREATE INDEX IF NOT EXISTS idx_positions_instrument ON positions(instrument, account_id);
                CREATE INDEX IF NOT EXISTS idx_position_events_balance ON position_events(instrument, account_id, recorded_at);
                CREATE INDEX IF NOT EXISTS idx_ca_instrument_status ON corporate_actions(instrument, status);
                CREATE INDEX IF NOT EXISTS idx_entitlements_ca ON entitlements(corporate_action_id, status);
                CREATE INDEX IF NOT EXISTS idx_reviews_ca ON instruction_reviews(corporate_action_id, status);
                CREATE UNIQUE INDEX IF NOT EXISTS idx_active_entitlement
                    ON entitlements(corporate_action_id, account_id)
                    WHERE status IN ('provisional','frozen','issued');
                CREATE UNIQUE INDEX IF NOT EXISTS idx_active_corporate_action
                    ON corporate_actions(instrument)
                    WHERE status IN ('draft','frozen');
                """
            )

    @staticmethod
    def _row(row: sqlite3.Row) -> Dict[str, Any]:
        item = dict(row)
        item["payload"] = json.loads(item["payload"])
        return item

    @staticmethod
    def _json(value: Dict[str, Any]) -> str:
        return json.dumps(value, ensure_ascii=False, sort_keys=True)

    def create(self, reference: str, state: str, payload: Dict[str, Any], actor_id: str) -> Dict[str, Any]:
        now = _now()
        try:
            with self._connect() as connection:
                cursor = connection.execute(
                    "INSERT INTO records(reference,state,version,payload,created_by,updated_by,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
                    (reference, state, 1, self._json(payload), actor_id, actor_id, now, now),
                )
                record_id = int(cursor.lastrowid)
                connection.execute(
                    "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
                    (record_id, "created", actor_id, 1, self._json({"state": state}), now),
                )
                row = connection.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
        except sqlite3.IntegrityError as exc:
            raise Conflict("reference已存在") from exc
        return self._row(row)

    def get(self, record_id: int) -> Dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
        if row is None:
            raise NotFound("结算指令不存在")
        return self._row(row)

    def list_records(self, state: Optional[str] = None, limit: int = 100) -> List[Dict[str, Any]]:
        limit = max(1, min(int(limit), 500))
        with self._connect() as connection:
            if state:
                rows = connection.execute("SELECT * FROM records WHERE state=? ORDER BY id DESC LIMIT ?", (state, limit)).fetchall()
            else:
                rows = connection.execute("SELECT * FROM records ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
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
                (state, version, self._json(payload), actor_id, now, record_id),
            )
            connection.execute(
                "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
                (record_id, action, actor_id, version, self._json(details), now),
            )
            result = connection.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
            connection.commit()
        return self._row(result)

    def _backfill_legacy_position_times(self, conn: sqlite3.Connection, instrument: str, record_time: str) -> int:
        cursor = conn.execute(
            "UPDATE positions SET recorded_at=?, updated_at=? WHERE instrument=? AND recorded_at IS NULL",
            (record_time, _now(), instrument),
        )
        conn.execute(
            "UPDATE position_events SET recorded_at=? WHERE instrument=? AND recorded_at IS NULL",
            (record_time, instrument),
        )
        return int(cursor.rowcount or 0)

    def _position_balances_as_of(self, conn: sqlite3.Connection, instrument: str, record_time: str) -> List[sqlite3.Row]:
        return conn.execute(
            """
            SELECT account_id, COALESCE(SUM(delta), 0) AS quantity
              FROM position_events
             WHERE instrument=? AND recorded_at IS NOT NULL AND recorded_at<=?
             GROUP BY account_id
            HAVING quantity > 0
             ORDER BY account_id
            """,
            (instrument, record_time),
        ).fetchall()

    def _replace_provisional_entitlements(self, conn: sqlite3.Connection, corporate_action: Dict[str, Any]) -> None:
        now = _now()
        ca_id = int(corporate_action["id"])
        payload = corporate_action["payload"]
        conn.execute(
            "UPDATE entitlements SET status='superseded', updated_at=? WHERE corporate_action_id=? AND status='provisional'",
            (now, ca_id),
        )
        rows = self._position_balances_as_of(conn, corporate_action["instrument"], payload["record_time"])
        for row in rows:
            amount = self._entitlement_values(int(row["quantity"]), payload)
            conn.execute(
                """
                INSERT INTO entitlements(
                    corporate_action_id,account_id,status,eligible_quantity,entitlement_quantity,
                    cash_amount,currency,version,created_at,updated_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    ca_id,
                    row["account_id"],
                    "provisional",
                    int(row["quantity"]),
                    amount["entitlement_quantity"],
                    amount["cash_amount"],
                    payload["currency"],
                    1,
                    now,
                    now,
                ),
            )

    @staticmethod
    def _entitlement_values(quantity: int, payload: Dict[str, Any]) -> Dict[str, Any]:
        if payload["action_type"] == "dividend":
            return {"entitlement_quantity": 0, "cash_amount": round(quantity * float(payload["ratio"]), 2)}
        return {"entitlement_quantity": int(quantity * float(payload["ratio"])), "cash_amount": 0.0}

    def _instruction_due(self, payload: Dict[str, Any]) -> Optional[str]:
        due = payload.get("settlement_due_at")
        if due:
            return due
        trade_at = payload.get("trade_at")
        if trade_at:
            from .rules import parse_datetime
            from datetime import timedelta
            parsed = datetime.fromisoformat(parse_datetime(trade_at, "trade_at"))
            return (parsed + timedelta(days=int(payload.get("settlement_day", 0)))).isoformat()
        return None

    def _refresh_instruction_reviews(self, conn: sqlite3.Connection, corporate_action: Dict[str, Any]) -> None:
        now = _now()
        ca_id = int(corporate_action["id"])
        record_time = corporate_action["payload"]["record_time"]
        rows = conn.execute("SELECT * FROM records WHERE json_extract(payload, '$.instrument')=? ORDER BY id", (corporate_action["instrument"],)).fetchall()
        for row in rows:
            payload = json.loads(row["payload"])
            due_at = self._instruction_due(payload)
            if not due_at or due_at > record_time:
                continue
            account_id = payload.get("account_id") or "DEFAULT"
            if row["state"] in ("settled", "reversed"):
                settled_at = payload.get("settled_at")
                if row["state"] == "settled" and settled_at and settled_at > record_time:
                    status, reason = "settled_after_record", "登记时点后完成交收，权益不转移"
                else:
                    continue
            elif row["state"] == "failed":
                status, reason, settled_at = "failed_after_record", "交收失败指令待核", None
            else:
                status, reason, settled_at = "pending", "登记时点前未完成交收，待核", None
            conn.execute(
                """
                INSERT INTO instruction_reviews(
                    corporate_action_id,instruction_id,account_id,side,quantity,status,reason,due_at,settled_at,created_at,updated_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(corporate_action_id, instruction_id) DO UPDATE SET
                    status=excluded.status, reason=excluded.reason, settled_at=excluded.settled_at, updated_at=excluded.updated_at
                WHERE instruction_reviews.status='pending'
                """,
                (ca_id, int(row["id"]), account_id, payload["side"], int(payload["quantity"]), status, reason, due_at, settled_at, now, now),
            )

    def _corporate_action_row(self, conn: sqlite3.Connection, corporate_action_id: int) -> sqlite3.Row:
        row = conn.execute("SELECT * FROM corporate_actions WHERE id=?", (corporate_action_id,)).fetchone()
        if row is None:
            raise NotFound("公司行动不存在")
        return row

    @staticmethod
    def _corporate_action_item(row: sqlite3.Row) -> Dict[str, Any]:
        item = dict(row)
        item["payload"] = json.loads(item["payload"])
        return item

    def create_position(self, instrument: str, account_id: str, quantity: int, recorded_at: Optional[str], actor_id: str) -> Dict[str, Any]:
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                connection.execute(
                    "INSERT INTO positions(instrument,account_id,quantity,recorded_at,created_at,updated_at) VALUES(?,?,?,?,?,?)",
                    (instrument, account_id, quantity, recorded_at, now, now),
                )
                connection.execute(
                    """
                    INSERT INTO position_events(
                        instrument,account_id,event_type,delta,recorded_at,event_key,request_id,instruction_id,created_at
                    ) VALUES(?,?,?,?,?,?,?,?,?)
                    """,
                    (instrument, account_id, "opening", quantity, recorded_at, "opening:%s:%s" % (instrument, account_id), None, None, now),
                )
                result = connection.execute("SELECT * FROM positions WHERE instrument=? AND account_id=?", (instrument, account_id)).fetchone()
                connection.commit()
            except sqlite3.IntegrityError as exc:
                connection.rollback()
                raise Conflict("持仓已存在") from exc
        return dict(result)

    def get_position(self, instrument: str, account_id: str) -> Dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM positions WHERE instrument=? AND account_id=?", (instrument, account_id)).fetchone()
        if row is None:
            raise NotFound("持仓不存在")
        return dict(row)

    def list_positions(self, instrument: Optional[str] = None, limit: int = 100) -> List[Dict[str, Any]]:
        limit = max(1, min(int(limit), 500))
        with self._connect() as connection:
            if instrument:
                rows = connection.execute(
                    "SELECT * FROM positions WHERE instrument=? ORDER BY instrument,account_id LIMIT ?", (instrument, limit)
                ).fetchall()
            else:
                rows = connection.execute("SELECT * FROM positions ORDER BY instrument,account_id LIMIT ?", (limit,)).fetchall()
        return [dict(row) for row in rows]

    def adjust_position(
        self,
        instrument: str,
        account_id: str,
        delta: int,
        recorded_at: str,
        request_id: str,
        reason: str,
        actor_id: str,
    ) -> Dict[str, Any]:
        now = _now()
        event_key = "request:%s" % request_id
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT * FROM position_events WHERE event_key=? OR (request_id=? AND request_id IS NOT NULL)",
                (event_key, request_id),
            ).fetchone()
            if existing:
                if existing["instrument"] != instrument or existing["account_id"] != account_id:
                    connection.rollback()
                    raise Conflict("request_id已用于其他持仓变更")
                result = connection.execute("SELECT * FROM positions WHERE instrument=? AND account_id=?", (instrument, account_id)).fetchone()
                connection.commit()
                return dict(result)
            row = connection.execute(
                "SELECT * FROM positions WHERE instrument=? AND account_id=?", (instrument, account_id)
            ).fetchone()
            if row is None:
                current = 0
                connection.execute(
                    "INSERT INTO positions(instrument,account_id,quantity,recorded_at,created_at,updated_at) VALUES(?,?,?,?,?,?)",
                    (instrument, account_id, 0, recorded_at, now, now),
                )
                connection.execute(
                    """
                    INSERT INTO position_events(
                        instrument,account_id,event_type,delta,recorded_at,event_key,request_id,instruction_id,created_at
                    ) VALUES(?,?,?,?,?,?,?,?,?)
                    """,
                    (instrument, account_id, "opening", 0, recorded_at, "opening:%s:%s" % (instrument, account_id), None, None, now),
                )
            else:
                current = int(row["quantity"])
            new_quantity = current + int(delta)
            if new_quantity < 0:
                connection.rollback()
                raise Conflict("调整后持仓不能为负")
            effective_recorded_at = recorded_at
            if row is not None and row["recorded_at"] and row["recorded_at"] > recorded_at:
                effective_recorded_at = row["recorded_at"]
            connection.execute(
                "UPDATE positions SET quantity=?,recorded_at=?,updated_at=? WHERE instrument=? AND account_id=?",
                (new_quantity, effective_recorded_at, now, instrument, account_id),
            )
            connection.execute(
                """
                INSERT INTO position_events(
                    instrument,account_id,event_type,delta,recorded_at,event_key,request_id,instruction_id,created_at
                ) VALUES(?,?,?,?,?,?,?,?,?)
                """,
                (instrument, account_id, "adjustment", delta, recorded_at, event_key, request_id, None, now),
            )
            self._recalculate_draft_corporate_actions(connection, instrument)
            result = connection.execute("SELECT * FROM positions WHERE instrument=? AND account_id=?", (instrument, account_id)).fetchone()
            connection.commit()
        return dict(result)

    def _recalculate_draft_corporate_actions(self, conn: sqlite3.Connection, instrument: str) -> None:
        now = _now()
        rows = conn.execute("SELECT * FROM corporate_actions WHERE instrument=? AND status='draft'", (instrument,)).fetchall()
        for row in rows:
            ca = self._corporate_action_item(row)
            self._replace_provisional_entitlements(conn, ca)
            conn.execute(
                "UPDATE corporate_actions SET version=version+1,updated_at=? WHERE id=?",
                (now, int(row["id"])),
            )

    def create_corporate_action(self, reference: str, payload: Dict[str, Any], actor_id: str) -> Dict[str, Any]:
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                backfilled = self._backfill_legacy_position_times(connection, payload["instrument"], payload["record_time"])
                active = connection.execute(
                    "SELECT id FROM corporate_actions WHERE instrument=? AND status IN ('draft','frozen') LIMIT 1",
                    (payload["instrument"],),
                ).fetchone()
                if active:
                    connection.rollback()
                    raise Conflict("同一证券已有未定稿公司行动")
                cursor = connection.execute(
                    """
                    INSERT INTO corporate_actions(
                        reference,instrument,status,version,payload,created_by,updated_by,created_at,updated_at
                    ) VALUES(?,?,?,?,?,?,?,?,?)
                    """,
                    (reference, payload["instrument"], "draft", 1, self._json(payload), actor_id, actor_id, now, now),
                )
                ca_id = int(cursor.lastrowid)
                ca = self._corporate_action_item(connection.execute("SELECT * FROM corporate_actions WHERE id=?", (ca_id,)).fetchone())
                self._replace_provisional_entitlements(connection, ca)
                self._refresh_instruction_reviews(connection, ca)
                if backfilled:
                    connection.execute(
                        "UPDATE corporate_actions SET payload=? WHERE id=?",
                        (self._json(dict(payload, legacy_position_record_time=payload["record_time"])), ca_id),
                    )
                result = connection.execute("SELECT * FROM corporate_actions WHERE id=?", (ca_id,)).fetchone()
                connection.commit()
            except sqlite3.IntegrityError as exc:
                connection.rollback()
                raise Conflict("公司行动reference已存在") from exc
        return self._corporate_action_item(result)

    def get_corporate_action(self, corporate_action_id: int) -> Dict[str, Any]:
        with self._connect() as connection:
            return self._corporate_action_item(self._corporate_action_row(connection, corporate_action_id))

    def list_corporate_actions(self, instrument: Optional[str] = None, status: Optional[str] = None, limit: int = 100) -> List[Dict[str, Any]]:
        clauses = []
        params: List[Any] = []
        if instrument:
            clauses.append("instrument=?")
            params.append(instrument)
        if status:
            clauses.append("status=?")
            params.append(status)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        params.append(max(1, min(int(limit), 500)))
        with self._connect() as connection:
            rows = connection.execute("SELECT * FROM corporate_actions%s ORDER BY id DESC LIMIT ?" % where, params).fetchall()
        return [self._corporate_action_item(row) for row in rows]

    def freeze_corporate_action(self, corporate_action_id: int, expected_version: int, request_id: str, actor_id: str) -> Dict[str, Any]:
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = self._corporate_action_row(connection, corporate_action_id)
            if row["freeze_request_id"] == request_id and row["status"] in ("frozen", "issued"):
                result = row
                connection.commit()
                return self._corporate_action_item(result)
            if int(row["version"]) != int(expected_version):
                connection.rollback()
                raise Conflict("版本冲突，请刷新后重试")
            if row["status"] != "draft":
                connection.rollback()
                raise Conflict("公司行动已定稿，不能重复冻结合格数量")
            ca = self._corporate_action_item(row)
            self._backfill_legacy_position_times(connection, ca["instrument"], ca["payload"]["record_time"])
            self._replace_provisional_entitlements(connection, ca)
            self._refresh_instruction_reviews(connection, ca)
            try:
                connection.execute(
                    """
                    UPDATE entitlements SET status='frozen', frozen_at=?, updated_at=?, version=version+1
                     WHERE corporate_action_id=? AND status='provisional'
                    """,
                    (now, now, corporate_action_id),
                )
                connection.execute(
                    """
                    UPDATE corporate_actions
                       SET status='frozen',version=version+1,payload=?,freeze_request_id=?,updated_by=?,updated_at=?,frozen_at=?
                     WHERE id=?
                    """,
                    (row["payload"], request_id, actor_id, now, now, corporate_action_id),
                )
                result = connection.execute("SELECT * FROM corporate_actions WHERE id=?", (corporate_action_id,)).fetchone()
                connection.commit()
            except sqlite3.IntegrityError as exc:
                connection.rollback()
                raise Conflict("冻结请求已用于其他公司行动") from exc
        return self._corporate_action_item(result)

    def issue_corporate_action(self, corporate_action_id: int, expected_version: int, request_id: str, actor_id: str) -> Dict[str, Any]:
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = self._corporate_action_row(connection, corporate_action_id)
            if row["issue_request_id"] == request_id and row["status"] == "issued":
                connection.commit()
                return self._corporate_action_item(row)
            if int(row["version"]) != int(expected_version):
                connection.rollback()
                raise Conflict("版本冲突，请刷新后重试")
            if row["status"] == "issued":
                connection.rollback()
                raise Conflict("公司行动权益已发放，不能重复发放")
            if row["status"] != "frozen":
                connection.rollback()
                raise Conflict("公司行动尚未冻结，不能发放")
            try:
                connection.execute(
                    """
                    UPDATE entitlements SET status='issued', issued_at=?, updated_at=?, version=version+1
                     WHERE corporate_action_id=? AND status='frozen'
                    """,
                    (now, now, corporate_action_id),
                )
                connection.execute(
                    "UPDATE corporate_actions SET status='issued',version=version+1,issue_request_id=?,updated_by=?,updated_at=?,issued_at=? WHERE id=?",
                    (request_id, actor_id, now, now, corporate_action_id),
                )
                result = connection.execute("SELECT * FROM corporate_actions WHERE id=?", (corporate_action_id,)).fetchone()
                connection.commit()
            except sqlite3.IntegrityError as exc:
                connection.rollback()
                raise Conflict("发放请求已用于其他公司行动") from exc
        return self._corporate_action_item(result)

    def list_entitlements(self, corporate_action_id: int, status: Optional[str] = None) -> List[Dict[str, Any]]:
        self.get_corporate_action(corporate_action_id)
        with self._connect() as connection:
            if status:
                rows = connection.execute(
                    "SELECT * FROM entitlements WHERE corporate_action_id=? AND status=? ORDER BY account_id",
                    (corporate_action_id, status),
                ).fetchall()
            else:
                rows = connection.execute(
                    "SELECT * FROM entitlements WHERE corporate_action_id=? AND status!='superseded' ORDER BY status,account_id",
                    (corporate_action_id,),
                ).fetchall()
        return [dict(row) for row in rows]

    def list_instruction_reviews(self, corporate_action_id: int, status: Optional[str] = None) -> List[Dict[str, Any]]:
        self.get_corporate_action(corporate_action_id)
        with self._connect() as connection:
            if status:
                rows = connection.execute(
                    "SELECT * FROM instruction_reviews WHERE corporate_action_id=? AND status=? ORDER BY id",
                    (corporate_action_id, status),
                ).fetchall()
            else:
                rows = connection.execute(
                    "SELECT * FROM instruction_reviews WHERE corporate_action_id=? ORDER BY id", (corporate_action_id,)
                ).fetchall()
        return [dict(row) for row in rows]

    def settle_instruction(
        self,
        record_id: int,
        expected_version: int,
        payload: Dict[str, Any],
        delivered_quantity: int,
        cash_paid: float,
        settled_at: str,
        request_id: Optional[str],
        actor_id: str,
        details: Dict[str, Any],
    ) -> Dict[str, Any]:
        now = _now()
        instrument = text_value(payload.get("instrument"), "instrument")
        account_id = payload.get("account_id") or "DEFAULT"
        quantity = int(payload["quantity"])
        delta = quantity if payload["side"] == "buy" else -quantity
        event_key = "settlement:%s" % record_id
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
            if row is None:
                connection.rollback()
                raise NotFound("结算指令不存在")
            current = self._row(row)
            if current["state"] == "settled" and request_id and current["payload"].get("settlement_request_id") == request_id:
                connection.commit()
                return current
            if int(row["version"]) != int(expected_version):
                connection.rollback()
                raise Conflict("版本冲突，请刷新后重试")
            if row["state"] != "approved":
                connection.rollback()
                raise Conflict("当前状态不允许执行settle")
            existing_event = connection.execute("SELECT id FROM position_events WHERE event_key=?", (event_key,)).fetchone()
            if existing_event:
                connection.rollback()
                raise Conflict("交收事件已入账，不能重复发放持仓变动")
            position = connection.execute(
                "SELECT * FROM positions WHERE instrument=? AND account_id=?", (instrument, account_id)
            ).fetchone()
            current_quantity = int(position["quantity"]) if position else 0
            new_quantity = current_quantity + delta
            if new_quantity < 0:
                connection.rollback()
                raise Conflict("交收后持仓不能为负")
            if position is None:
                connection.execute(
                    "INSERT INTO positions(instrument,account_id,quantity,recorded_at,created_at,updated_at) VALUES(?,?,?,?,?,?)",
                    (instrument, account_id, 0, settled_at, now, now),
                )
                connection.execute(
                    """
                    INSERT INTO position_events(
                        instrument,account_id,event_type,delta,recorded_at,event_key,request_id,instruction_id,created_at
                    ) VALUES(?,?,?,?,?,?,?,?,?)
                    """,
                    (instrument, account_id, "opening", 0, settled_at, "opening:%s:%s" % (instrument, account_id), None, None, now),
                )
            connection.execute(
                """
                INSERT INTO position_events(
                    instrument,account_id,event_type,delta,recorded_at,event_key,request_id,instruction_id,created_at
                ) VALUES(?,?,?,?,?,?,?,?,?)
                """,
                (instrument, account_id, "settlement", delta, settled_at, event_key, request_id, record_id, now),
            )
            connection.execute(
                "UPDATE positions SET quantity=?,recorded_at=COALESCE(recorded_at,?),updated_at=? WHERE instrument=? AND account_id=?",
                (new_quantity, settled_at, now, instrument, account_id),
            )
            connection.execute(
                "UPDATE records SET state='settled',version=?,payload=?,updated_by=?,updated_at=? WHERE id=?",
                (int(expected_version) + 1, self._json(payload), actor_id, now, record_id),
            )
            connection.execute(
                "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
                (record_id, "settle", actor_id, int(expected_version) + 1, self._json(details), now),
            )
            connection.execute(
                """
                UPDATE instruction_reviews
                   SET status=CASE
                           WHEN corporate_actions.status='draft' AND json_extract(corporate_actions.payload, '$.record_time') >= ?
                               THEN 'reviewed'
                           ELSE 'settled_after_record'
                       END,
                       reason=CASE
                           WHEN corporate_actions.status='draft' AND json_extract(corporate_actions.payload, '$.record_time') >= ?
                               THEN '登记时点前完成交收，权益转移'
                           ELSE '登记时点后完成交收，权益不转移'
                       END,
                       settled_at=?,
                       updated_at=?
                  FROM corporate_actions
                 WHERE instruction_reviews.corporate_action_id=corporate_actions.id
                   AND instruction_reviews.instruction_id=?
                   AND instruction_reviews.status='pending'
                   AND corporate_actions.instrument=?
                """,
                (settled_at, settled_at, settled_at, now, record_id, instrument),
            )
            self._recalculate_draft_corporate_actions(connection, instrument)
            result = connection.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
            connection.commit()
        return self._row(result)

    def fail_instruction(
        self,
        record_id: int,
        expected_version: int,
        payload: Dict[str, Any],
        reason: str,
        actor_id: str,
        details: Dict[str, Any],
    ) -> Dict[str, Any]:
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
            if row is None:
                connection.rollback()
                raise NotFound("结算指令不存在")
            if int(row["version"]) != int(expected_version):
                connection.rollback()
                raise Conflict("版本冲突，请刷新后重试")
            if row["state"] != "approved":
                connection.rollback()
                raise Conflict("当前状态不允许执行fail")
            connection.execute(
                "UPDATE records SET state='failed',version=?,payload=?,updated_by=?,updated_at=? WHERE id=?",
                (int(expected_version) + 1, self._json(payload), actor_id, now, record_id),
            )
            connection.execute(
                "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
                (record_id, "fail", actor_id, int(expected_version) + 1, self._json(details), now),
            )
            connection.execute(
                """
                UPDATE instruction_reviews
                   SET status='failed_after_record', updated_at=?
                 WHERE instruction_id=?
                   AND status='pending'
                   AND corporate_action_id IN (
                       SELECT id FROM corporate_actions WHERE instrument=? AND status IN ('draft','frozen','issued')
                   )
                """,
                (now, record_id, payload.get("instrument")),
            )
            result = connection.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
            connection.commit()
        return self._row(result)

    def add_audit(self, record_id: int, actor_id: str, action: str, details: Dict[str, Any]) -> None:
        with self._connect() as connection:
            row = connection.execute("SELECT version FROM records WHERE id=?", (record_id,)).fetchone()
            if row is None:
                raise NotFound("记录不存在")
            connection.execute(
                "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
                (record_id, action, actor_id, int(row["version"]), self._json(details), _now()),
            )

    def audit_timeline(self, record_id: int) -> List[Dict[str, Any]]:
        self.get(record_id)
        with self._connect() as connection:
            rows = connection.execute("SELECT * FROM audit_events WHERE record_id=? ORDER BY id", (record_id,)).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["details"] = json.loads(item["details"])
            result.append(item)
        return result

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


def text_value(value: Any, key: str) -> str:
    if not isinstance(value, str) or not value.strip():
        from .domain import ValidationError
        raise ValidationError("%s不能为空" % key)
    return value.strip()
