"""领域契约的基础回归测试。"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from domain_contract.validator import load_contract, summarize


class ContractTest(unittest.TestCase):
    def test_contract_is_complete(self) -> None:
        value = load_contract(ROOT / "domain" / "contract.json")
        result = summarize(value)
        self.assertGreaterEqual(result["actor_count"], 3)
        self.assertGreaterEqual(result["state_count"], 6)
        self.assertGreaterEqual(result["invariant_count"], 4)
        self.assertEqual(result["case_count"], 2)

    def test_state_transitions_cover_lifecycle(self) -> None:
        value = load_contract(ROOT / "domain" / "contract.json")
        actions = value["state_transitions"]
        # 调解、转执法、撤回、复开均在契约中显式约束
        for action in ("调解", "转执法", "撤回", "复开"):
            self.assertIn(action, actions)
        # 撤回可发生在登记/待核验/处置中；复开只能来自已撤回或已归档
        self.assertEqual(set(actions["撤回"]["from"]), {"登记", "待核验", "处置中"})
        self.assertEqual(set(actions["复开"]["from"]), {"已撤回", "已归档"})
        # 调解不脱离处置中
        self.assertEqual(actions["调解"]["to"], "处置中")
        self.assertEqual(actions["调解"]["from"], ["处置中"])


if __name__ == "__main__":
    unittest.main()
