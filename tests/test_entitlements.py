"""登记时点冻结、权益归属、并发互斥、幂等重试与旧持仓补齐测试。"""
import tempfile
import threading
import unittest
from pathlib import Path

from app import build_service
from src.domain import Actor, Conflict

OFFICER = lambda: Actor("officer", "settlement_officer")
CA_OFFICER = lambda: Actor("ca-officer", "corporate_actions")


def create_instruction(service, reference, side, quantity=1000, price=10.0):
    data = {
        'instrument': 'ACME', 'side': side, 'quantity': quantity, 'price': price,
        'fees': 0.0, 'currency': 'CNY', 'settlement_day': 2,
        'corporate_action': 'none', 'action_ratio': 1.0,
    }
    if side == 'sell':
        data['seller_account'] = 'FundA'
        data['buyer_account'] = 'MAIN'
    else:
        data['buyer_account'] = 'MAIN'
        data['seller_account'] = 'FundA'
    return service.create(Actor("trader", "trader"), reference, data)


def approve_and_settle(service, record, settled_at):
    record = service.act(OFFICER(), record["id"], record["version"], "approve", {})
    return service.act(
        OFFICER(), record["id"], record["version"], "settle",
        {"delivered_quantity": record["payload"]["quantity"], "cash_paid": record["payload"]["net_amount"], "settled_at": settled_at},
    )


class EntitlementTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.service = build_service(str(Path(self.temp.name) / "test.db"))
        # FundA 期初持有 1000 股，登记时点早于公司行动
        self.service.create_holding(OFFICER(), payload={"account": "FundA", "instrument": "ACME", "quantity": 1000, "recorded_at": "2026-09-20T08:00:00+00:00"})
        self.record_date = "2026-09-30T08:00:00+00:00"

    def tearDown(self):
        self.temp.cleanup()

    def _dividend_ca(self, reference="CA-1", cash_rate=0.5):
        return self.service.create_corporate_action(
            CA_OFFICER(), reference,
            {"instrument": "ACME", "action_type": "dividend", "ratio": 0, "cash_rate": cash_rate, "record_date": self.record_date},
        )

    def test_pending_instruction_transfer_not_confirmed(self):
        """登记时点未完成交收：过户未确认，权益不随买方走，原持有人仍享权益。"""
        sell = create_instruction(self.service, "TRD-SELL-1", "sell")
        self.service.act(OFFICER(), sell["id"], sell["version"], "approve", {})
        ca = self._dividend_ca()
        result = self.service.calculate_entitlements(CA_OFFICER(), ca["id"], ca["version"])
        accounts = {row["account"]: row for row in result["entitlements"]}
        self.assertIn("FundA", accounts)                       # 原持有人
        self.assertEqual(accounts["FundA"]["cash_amount"], 500.0)
        self.assertNotIn("MAIN", accounts)                     # 买方未取得权益
        self.assertEqual([item["reference"] for item in result["ca"]["summary"]["pending"]], ["TRD-SELL-1"])

    def test_settled_before_record_date_transfers_entitlement(self):
        """时点前完成交收：权益随过户转移给买方。"""
        sell = create_instruction(self.service, "TRD-SELL-2", "sell")
        buy = create_instruction(self.service, "TRD-BUY-2", "buy")
        approve_and_settle(self.service, sell, "2026-09-25T10:00:00+00:00")
        approve_and_settle(self.service, buy, "2026-09-25T10:00:00+00:00")
        ca = self._dividend_ca()
        result = self.service.calculate_entitlements(CA_OFFICER(), ca["id"], ca["version"])
        accounts = {row["account"]: row for row in result["entitlements"]}
        self.assertNotIn("FundA", accounts)                    # 已全部卖出
        self.assertEqual(accounts["MAIN"]["cash_amount"], 500.0)
        self.assertEqual(result["ca"]["summary"]["pending"], [])

    def test_late_settlement_cannot_change_issued_entitlement(self):
        """时点后才交收：进 late 清单，已发权益不倒改。"""
        sell = create_instruction(self.service, "TRD-SELL-3", "sell")
        approve_and_settle(self.service, sell, "2026-10-02T10:00:00+00:00")  # 登记日后才过户
        ca = self._dividend_ca()
        result = self.service.finalize_corporate_action(CA_OFFICER(), ca["id"], ca["version"])
        accounts = {row["account"]: row for row in result["entitlements"]}
        self.assertEqual(accounts["FundA"]["cash_amount"], 500.0)
        self.assertNotIn("MAIN", accounts)
        self.assertEqual([item["reference"] for item in result["late"]], ["TRD-SELL-3"])
        # 定稿后任何重算/再定稿都被拒绝
        with self.assertRaises(Conflict):
            self.service.calculate_entitlements(CA_OFFICER(), ca["id"], 2)
        finalized_ca = self.service.get_corporate_action(CA_OFFICER(), ca["id"])
        again = self.service.finalize_corporate_action(CA_OFFICER(), ca["id"], finalized_ca["version"])
        self.assertIn("note", again)
        with self.assertRaises(Conflict):
            self.service.finalize_corporate_action(CA_OFFICER(), ca["id"], 99)
        stored = self.service.entitlements(CA_OFFICER(), ca["id"])
        self.assertEqual(len(stored), 1)
        self.assertEqual(stored[0]["status"], "issued")

    def test_concurrent_finalize_only_one_wins(self):
        """两位专员同时定稿同一证券：只放行一位，后到者版本冲突。"""
        ca = self._dividend_ca("CA-CONC")
        outcomes = []

        def worker():
            try:
                result = self.service.finalize_corporate_action(CA_OFFICER(), ca["id"], 1)
                outcomes.append(("ok", len(result["entitlements"])))
            except Conflict as exc:
                outcomes.append(("conflict", str(exc)))
            except Exception as exc:  # noqa: BLE001
                outcomes.append(("error", repr(exc)))

        threads = [threading.Thread(target=worker) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        statuses = sorted(item[0] for item in outcomes)
        self.assertEqual(statuses, ["conflict", "ok"])
        stored = self.service.entitlements(CA_OFFICER(), ca["id"])
        self.assertEqual([row["status"] for row in stored], ["issued"])

    def test_retry_after_write_failure_is_idempotent(self):
        """失败重试沿用原指令：相同幂等键不重复发放。"""
        ca = self._dividend_ca("CA-IDEM")
        first = self.service.finalize_corporate_action(CA_OFFICER(), ca["id"], ca["version"], idempotency_key="batch-001")
        self.assertEqual(first["entitlements"][0]["cash_amount"], 500.0)
        retry = self.service.finalize_corporate_action(CA_OFFICER(), ca["id"], ca["version"], idempotency_key="batch-001")
        self.assertEqual(retry["entitlements"], first["entitlements"])
        stored = self.service.entitlements(CA_OFFICER(), ca["id"])
        self.assertEqual(len(stored), 1)
        self.assertEqual(stored[0]["cash_amount"], 500.0)

    def test_holding_change_invalidates_draft_immediately(self):
        """持仓数量变化后，未定稿权益立即失效并抬升版本，必须重算。"""
        ca = self._dividend_ca("CA-DRAFT")
        calculated = self.service.calculate_entitlements(CA_OFFICER(), ca["id"], ca["version"])
        self.assertEqual(calculated["ca"]["version"], 2)
        # 重算用旧版本 -> 冲突
        with self.assertRaises(Conflict):
            self.service.calculate_entitlements(CA_OFFICER(), ca["id"], 1)

        holding = self.service.list_holdings(OFFICER(), instrument="ACME")[0]
        self.service.adjust_holding(OFFICER(), holding["id"], holding["version"], {"delta": 200, "reason": "补账"})
        ca_row = self.service.get_corporate_action(CA_OFFICER(), ca["id"])
        self.assertEqual(ca_row["summary"]["phase"], "stale")
        self.assertGreater(ca_row["version"], 2)
        self.assertEqual(self.service.entitlements(CA_OFFICER(), ca["id"]), [])
        # 按新版本重算：1200 股 * 0.5
        recalculated = self.service.calculate_entitlements(CA_OFFICER(), ca["id"], ca_row["version"])
        self.assertEqual(recalculated["entitlements"][0]["cash_amount"], 600.0)

    def test_settlement_after_draft_invalidates_and_recalculates(self):
        """草稿算出后，时点前完成交收 -> 草稿失效，重算后权益转给买方。"""
        ca = self._dividend_ca("CA-REVAL")
        self.service.calculate_entitlements(CA_OFFICER(), ca["id"], ca["version"])
        sell = create_instruction(self.service, "TRD-REVAL-S", "sell")
        buy = create_instruction(self.service, "TRD-REVAL-B", "buy")
        approve_and_settle(self.service, sell, "2026-09-28T10:00:00+00:00")
        approve_and_settle(self.service, buy, "2026-09-28T10:00:00+00:00")
        ca_row = self.service.get_corporate_action(CA_OFFICER(), ca["id"])
        self.assertEqual(ca_row["summary"]["phase"], "stale")
        self.assertEqual(self.service.entitlements(CA_OFFICER(), ca["id"]), [])
        result = self.service.calculate_entitlements(CA_OFFICER(), ca["id"], ca_row["version"])
        accounts = {row["account"]: row for row in result["entitlements"]}
        self.assertNotIn("FundA", accounts)
        self.assertEqual(accounts["MAIN"]["cash_amount"], 500.0)

    def test_legacy_holding_record_point_backfilled(self):
        """旧持仓缺登记时点：按首次公司行动的登记时点补齐后计入合格数量。"""
        self.service.create_holding(OFFICER(), payload={"account": "LegacyFund", "instrument": "OLD", "quantity": 300})
        ca = self.service.create_corporate_action(
            CA_OFFICER(), "CA-OLD-1",
            {"instrument": "OLD", "action_type": "split", "ratio": 2.0, "record_date": self.record_date},
        )
        holding = self.service.list_holdings(OFFICER(), instrument="OLD")[0]
        self.assertEqual(holding["recorded_at"], self.record_date)
        result = self.service.calculate_entitlements(CA_OFFICER(), ca["id"], ca["version"])
        self.assertEqual(result["entitlements"][0]["account"], "LegacyFund")
        self.assertEqual(result["entitlements"][0]["quantity"], 600)

        # 第二个公司行动不改变已补齐的时点；新建旧持仓也不回填到后来的行动
        self.service.create_holding(OFFICER(), payload={"account": "LegacyFund2", "instrument": "OLD", "quantity": 100})
        ca2 = self.service.create_corporate_action(
            CA_OFFICER(), "CA-OLD-2",
            {"instrument": "OLD", "action_type": "split", "ratio": 3.0, "record_date": "2026-12-01T08:00:00+00:00"},
        )
        h2 = self.service.get_holding(OFFICER(), self.service.list_holdings(OFFICER(), instrument="OLD", account="LegacyFund2")[0]["id"])
        self.assertEqual(h2["recorded_at"], "2026-12-01T08:00:00+00:00")
        result2 = self.service.calculate_entitlements(CA_OFFICER(), ca2["id"], ca2["version"])
        by_account = {row["account"]: row["quantity"] for row in result2["entitlements"]}
        self.assertEqual(by_account, {"LegacyFund": 900, "LegacyFund2": 300})


if __name__ == "__main__":
    unittest.main()
