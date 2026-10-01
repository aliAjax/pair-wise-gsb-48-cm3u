import tempfile
import unittest
from pathlib import Path

from app import build_service
from src.domain import Actor, Conflict, PermissionDenied, ValidationError


OFFICER = Actor("officer", "settlement_officer")
CA_OFFICER = Actor("ca-1", "corporate_actions")
CA_OFFICER_2 = Actor("ca-2", "corporate_actions")
TRADER = Actor("trader-1", "trader")


def instruction(reference: str, **overrides):
    data = {
        'instrument': 'ACME',
        'side': 'buy',
        'quantity': 100,
        'price': 10.0,
        'fees': 0.0,
        'currency': 'CNY',
        'settlement_day': 2,
        'corporate_action': 'none',
        'action_ratio': 1.0,
        'account_id': 'A',
        'trade_at': '2026-01-01T08:00:00Z',
        'settlement_due_at': '2026-01-03T08:00:00Z',
    }
    data.update(overrides)
    return TRADER, reference, data


class CorporateActionWorkflowTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.service = build_service(str(Path(self.temp.name) / "test.db"))

    def tearDown(self):
        self.temp.cleanup()

    def approve(self, record):
        return self.service.act(OFFICER, record["id"], record["version"], "approve", {})

    def settle(self, record, settled_at, request_id="settle-1", delivered=None, paid=None):
        payload = record["payload"]
        delivered = payload["quantity"] if delivered is None else delivered
        paid = payload["net_amount"] if paid is None else paid
        return self.service.act(
            OFFICER,
            record["id"],
            record["version"],
            "settle",
            {
                "delivered_quantity": delivered,
                "cash_paid": paid,
                "settled_at": settled_at,
                "request_id": request_id,
            },
        )

    def create_ca(self, reference="CA-1", record_time="2026-01-05T08:00:00Z", action_type="split", ratio=2.0):
        return self.service.create_corporate_action(
            CA_OFFICER,
            reference,
            {
                "instrument": "ACME",
                "action_type": action_type,
                "ratio": ratio,
                "record_time": record_time,
                "currency": "CNY",
            },
        )

    def test_settlement_before_record_transfers_entitlement_and_later_cannot_change_issued(self):
        self.service.create_position(OFFICER, {"instrument": "ACME", "account_id": "A", "quantity": 100, "recorded_at": "2026-01-01T08:00:00Z"})
        _, _, buy_data = instruction("TRD-1", account_id="B")
        buy = self.service.create(TRADER, "TRD-1", buy_data)
        buy = self.approve(buy)
        self.settle(buy, "2026-01-04T08:00:00Z")

        ca = self.create_ca()
        self.assertEqual([e["account_id"] for e in self.service.list_entitlements(CA_OFFICER, ca["id"])], ["A", "B"])

        ca = self.service.freeze_corporate_action(CA_OFFICER, ca["id"], ca["version"], {"request_id": "freeze-1"})
        frozen = {e["account_id"]: e for e in self.service.list_entitlements(CA_OFFICER, ca["id"], "frozen")}
        self.assertEqual(frozen["A"]["eligible_quantity"], 100)
        self.assertEqual(frozen["B"]["eligible_quantity"], 100)
        self.assertEqual(frozen["B"]["entitlement_quantity"], 200)

        ca = self.service.issue_corporate_action(CA_OFFICER, ca["id"], ca["version"], {"request_id": "issue-1"})
        issued = {e["account_id"]: e for e in self.service.list_entitlements(CA_OFFICER, ca["id"], "issued")}
        self.assertEqual(issued["B"]["status"], "issued")

        # 登记时点之后才完成的卖单，不冲回已发给买方的权益。
        _, _, sell_data = instruction("TRD-2", side="sell", account_id="A", quantity=100, settlement_due_at="2026-01-06T08:00:00Z")
        sell = self.service.create(TRADER, "TRD-2", sell_data)
        sell = self.approve(sell)
        self.settle(sell, "2026-01-06T08:00:00Z", request_id="settle-2")
        still_issued = {e["account_id"]: e for e in self.service.list_entitlements(CA_OFFICER, ca["id"], "issued")}
        self.assertEqual(still_issued["A"]["eligible_quantity"], 100)
        self.assertEqual(still_issued["B"]["eligible_quantity"], 100)

    def test_pending_instruction_is_reviewed_and_late_settlement_does_not_transfer(self):
        self.service.create_position(OFFICER, {"instrument": "ACME", "account_id": "A", "quantity": 100, "recorded_at": "2026-01-01T08:00:00Z"})
        _, _, pending_data = instruction("TRD-PENDING", account_id="B")
        pending = self.service.create(TRADER, "TRD-PENDING", pending_data)
        pending = self.approve(pending)

        ca = self.create_ca()
        reviews = self.service.list_instruction_reviews(CA_OFFICER, ca["id"])
        self.assertEqual(len(reviews), 1)
        self.assertEqual(reviews[0]["status"], "pending")
        self.assertEqual(reviews[0]["reason"], "登记时点前未完成交收，待核")

        ca = self.service.freeze_corporate_action(CA_OFFICER, ca["id"], ca["version"], {"request_id": "freeze-1"})
        frozen_accounts = [e["account_id"] for e in self.service.list_entitlements(CA_OFFICER, ca["id"], "frozen")]
        self.assertEqual(frozen_accounts, ["A"])

        self.settle(pending, "2026-01-06T08:00:00Z", request_id="late")
        reviews = {r["instruction_id"]: r for r in self.service.list_instruction_reviews(CA_OFFICER, ca["id"])}
        self.assertEqual(reviews[pending["id"]]["status"], "settled_after_record")
        frozen = self.service.list_entitlements(CA_OFFICER, ca["id"], "frozen")
        self.assertEqual([e["account_id"] for e in frozen], ["A"])

    def test_second_specialist_freezing_same_action_gets_version_conflict(self):
        self.service.create_position(OFFICER, {"instrument": "ACME", "account_id": "A", "quantity": 100, "recorded_at": "2026-01-01T08:00:00Z"})
        ca = self.create_ca()
        first = self.service.freeze_corporate_action(CA_OFFICER, ca["id"], ca["version"], {"request_id": "freeze-1"})
        self.assertEqual(first["status"], "frozen")
        with self.assertRaises(Conflict):
            self.service.freeze_corporate_action(CA_OFFICER_2, ca["id"], ca["version"], {"request_id": "freeze-2"})

    def test_two_specialists_cannot_open_processing_for_same_instrument(self):
        self.create_ca("CA-1")
        with self.assertRaises(Conflict):
            self.service.create_corporate_action(CA_OFFICER_2, "CA-2", {
                "instrument": "ACME",
                "action_type": "dividend",
                "ratio": 0.1,
                "record_time": "2026-01-05T08:00:00Z",
                "currency": "CNY",
            })

    def test_early_settlement_after_draft_action_transfers_and_closes_review(self):
        self.service.create_position(OFFICER, {"instrument": "ACME", "account_id": "A", "quantity": 100, "recorded_at": "2026-01-01T08:00:00Z"})
        _, _, pending_data = instruction("TRD-EARLY", account_id="B")
        pending = self.service.create(TRADER, "TRD-EARLY", pending_data)
        pending = self.approve(pending)
        ca = self.create_ca()
        self.assertEqual(self.service.list_entitlements(CA_OFFICER, ca["id"])[0]["account_id"], "A")

        ca = self.service.get_corporate_action(CA_OFFICER, ca["id"])
        settled = self.settle(pending, "2026-01-04T08:00:00Z", request_id="early")
        review = self.service.list_instruction_reviews(CA_OFFICER, ca["id"])[0]
        self.assertEqual(review["status"], "reviewed")
        accounts = {e["account_id"] for e in self.service.list_entitlements(CA_OFFICER, ca["id"])}
        self.assertEqual(accounts, {"A", "B"})
        self.assertEqual(settled["state"], "settled")

    def test_failed_write_retry_uses_original_request_and_does_not_double_issue(self):
        self.service.create_position(OFFICER, {"instrument": "ACME", "account_id": "A", "quantity": 100, "recorded_at": "2026-01-01T08:00:00Z"})
        _, _, data = instruction("TRD-IDEMPOTENT")
        record = self.service.create(TRADER, "TRD-IDEMPOTENT", data)
        record = self.approve(record)
        first = self.settle(record, "2026-01-04T08:00:00Z", request_id="same-request")
        retry = self.settle(record, "2026-01-04T08:00:00Z", request_id="same-request")
        self.assertEqual(retry["version"], first["version"])
        position = self.service.get_position(CA_OFFICER, "ACME", "A")
        self.assertEqual(position["quantity"], 200)

        ca = self.create_ca()
        ca = self.service.freeze_corporate_action(CA_OFFICER, ca["id"], ca["version"], {"request_id": "freeze-same"})
        issued = self.service.issue_corporate_action(CA_OFFICER, ca["id"], ca["version"], {"request_id": "issue-same"})
        retried_issue = self.service.issue_corporate_action(CA_OFFICER, issued["id"], issued["version"], {"request_id": "issue-same"})
        self.assertEqual(retried_issue["version"], issued["version"])
        entitlements = self.service.list_entitlements(CA_OFFICER, ca["id"], "issued")
        self.assertEqual(len(entitlements), 1)
        self.assertEqual(entitlements[0]["entitlement_quantity"], 400)

    def test_position_change_invalidates_and_recalculates_unfinalised_entitlement(self):
        self.service.create_position(OFFICER, {"instrument": "ACME", "account_id": "A", "quantity": 100, "recorded_at": "2026-01-01T08:00:00Z"})
        ca = self.create_ca(action_type="dividend", ratio=0.5)
        before = self.service.list_entitlements(CA_OFFICER, ca["id"])[0]
        self.assertEqual(before["cash_amount"], 50.0)

        # 版本递增；旧未定稿权益被标记失效，新权益按新持仓重算。
        self.service.adjust_position(OFFICER, {
            "instrument": "ACME",
            "account_id": "A",
            "delta": 30,
            "recorded_at": "2026-01-02T08:00:00Z",
            "reason": "补录",
            "request_id": "adjust-1",
        })
        changed = self.service.get_corporate_action(CA_OFFICER, ca["id"])
        self.assertEqual(changed["version"], ca["version"] + 1)
        active = self.service.list_entitlements(CA_OFFICER, ca["id"])
        self.assertEqual(len(active), 1)
        self.assertEqual(active[0]["status"], "provisional")
        self.assertEqual(active[0]["eligible_quantity"], 130)
        self.assertEqual(active[0]["cash_amount"], 65.0)

    def test_legacy_position_record_time_is_backfilled_from_first_corporate_action(self):
        self.service.create_position(OFFICER, {"instrument": "ACME", "account_id": "A", "quantity": 100})
        position_before = self.service.get_position(CA_OFFICER, "ACME", "A")
        self.assertIsNone(position_before["recorded_at"])

        ca = self.create_ca(record_time="2026-01-05T08:00:00Z")
        position_after = self.service.get_position(CA_OFFICER, "ACME", "A")
        self.assertEqual(position_after["recorded_at"], "2026-01-05T08:00:00+00:00")
        entitlement = self.service.list_entitlements(CA_OFFICER, ca["id"])[0]
        self.assertEqual(entitlement["eligible_quantity"], 100)

    def test_post_cutoff_position_change_does_not_enter_record_time_balance(self):
        self.service.create_position(OFFICER, {"instrument": "ACME", "account_id": "A", "quantity": 100, "recorded_at": "2026-01-01T08:00:00Z"})
        ca = self.create_ca()
        self.service.adjust_position(OFFICER, {
            "instrument": "ACME",
            "account_id": "A",
            "delta": 40,
            "recorded_at": "2026-01-06T08:00:00Z",
            "reason": "登记后转入",
            "request_id": "post-cutoff",
        })
        entitlements = self.service.list_entitlements(CA_OFFICER, ca["id"])
        self.assertEqual(entitlements[0]["eligible_quantity"], 100)
        self.assertEqual(self.service.get_position(CA_OFFICER, "ACME", "A")["quantity"], 140)

    def test_issued_ca_cannot_be_reissued_with_another_request(self):
        self.service.create_position(OFFICER, {"instrument": "ACME", "account_id": "A", "quantity": 100, "recorded_at": "2026-01-01T08:00:00Z"})
        ca = self.create_ca(action_type="dividend", ratio=0.5)
        ca = self.service.freeze_corporate_action(CA_OFFICER, ca["id"], ca["version"], {"request_id": "freeze"})
        issued = self.service.issue_corporate_action(CA_OFFICER, ca["id"], ca["version"], {"request_id": "issue-1"})
        with self.assertRaises(Conflict):
            self.service.issue_corporate_action(CA_OFFICER_2, issued["id"], issued["version"], {"request_id": "issue-2"})

    def test_position_endpoint_permissions(self):
        with self.assertRaises(PermissionDenied):
            self.service.create_position(CA_OFFICER, {"instrument": "ACME", "account_id": "A", "quantity": 1})

    def test_adjustment_request_id_is_idempotent(self):
        payload = {
            "instrument": "ACME",
            "account_id": "A",
            "delta": 10,
            "recorded_at": "2026-01-02T08:00:00Z",
            "reason": "补录",
            "request_id": "pos-req-1",
        }
        self.service.create_position(OFFICER, {"instrument": "ACME", "account_id": "A", "quantity": 100, "recorded_at": "2026-01-01T08:00:00Z"})
        self.service.adjust_position(OFFICER, payload)
        self.service.adjust_position(OFFICER, payload)
        self.assertEqual(self.service.get_position(CA_OFFICER, "ACME", "A")["quantity"], 110)

    def test_same_position_request_id_cannot_change_target(self):
        self.service.create_position(OFFICER, {"instrument": "ACME", "account_id": "A", "quantity": 100, "recorded_at": "2026-01-01T08:00:00Z"})
        payload = {
            "instrument": "ACME",
            "account_id": "A",
            "delta": 10,
            "recorded_at": "2026-01-02T08:00:00Z",
            "reason": "补录",
            "request_id": "same-target",
        }
        self.service.adjust_position(OFFICER, payload)
        payload["account_id"] = "B"
        with self.assertRaises(Conflict):
            self.service.adjust_position(OFFICER, payload)
