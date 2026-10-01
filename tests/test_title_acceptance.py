from __future__ import annotations

import unittest
from pathlib import Path

from battery_title.acceptance import run


ROOT = Path(__file__).resolve().parents[1]


class TitleAcceptanceTests(unittest.TestCase):
    def test_offline_acceptance(self) -> None:
        result = run(ROOT)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["frozen_sha256_length"], 64)
        # 条件未齐时被阻断，且明确给出每个被阻断原因
        self.assertIn("awaiting_consent:@acceptance", result["blocked_before_consents"])
        self.assertIn("unresolved_restriction:pledge-1:pledge", result["blocked_before_consents"])
        # 三权在交割时原子转移
        self.assertEqual(result["owner_after_settlement"], "buyer-co")
        self.assertEqual(result["custodian_after_settlement"], "site-b")
        self.assertEqual(result["operator_after_settlement"], "buyer-co")
        self.assertEqual(result["restrictions_released_at_settlement"], [])
        # 重试不产生第二次转让
        self.assertTrue(result["settled_replay_same_events"])
        # 竞争交易有确定赢家
        self.assertEqual(result["rival_state"], "failed")
        self.assertEqual(result["rival_block_reason"], "lost_contest:deal-main")
        # 退回是反向事实，三权与质押恢复，历史保留
        self.assertEqual(result["owner_after_reverse"], "seller-co")
        self.assertEqual(result["custodian_after_reverse"], "site-a")
        self.assertEqual(result["operator_after_reverse"], "om-co")
        self.assertEqual(result["pledge_restored_after_reverse"], ["pledge-1"])
        self.assertEqual(
            result["rights_chain"],
            ["enrolled", "restriction_put_on", "title_changed", "title_reversed"],
        )
        # 依据版本变化：失败保留已完成同意，不提前改变权属
        self.assertEqual(result["stale_failure_reasons"], ["stale_basis:batt-002"])
        self.assertEqual(result["stale_consents_preserved"], 1)
        self.assertEqual(result["stale_owner_unchanged"], "seller-co")
        self.assertTrue(result["audit"]["valid"])


if __name__ == "__main__":
    unittest.main()
