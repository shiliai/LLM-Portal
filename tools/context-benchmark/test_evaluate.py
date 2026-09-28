from __future__ import annotations

import json
import sys
import unittest
from io import StringIO
from pathlib import Path


ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
import evaluate  # noqa: E402


class EvaluateTest(unittest.TestCase):
    def _record(self) -> dict:
        repeated = "\x1b[31mERROR\x1b[0m\n" + "same output\n" * 5 + "done"
        large_tool_output = "\x1b[31mheader\x1b[0m\n" + "".join(f"tool line {index:04d}\n" for index in range(5000)) + "tail"
        return {
            "sample_id": "ctx-0000000000000001",
            "protocol": "openai_chat",
            "aggregate": {"tool_result_count": 0},
            "replay": {
                "endpoint": "/v1/chat/completions",
                "body": {
                    "model": "fixture",
                    "system": "top level system marker",
                    "messages": [
                        {"role": "system", "content": "system\nline\nsame"},
                        {"role": "user", "content": repeated},
                        {
                            "role": "assistant",
                            "tool_calls": [{
                                "id": "call_1",
                                "type": "function",
                                "function": {
                                    "name": "run",
                                    "arguments": '{"command":"echo same\\nsame"}',
                                },
                            }],
                        },
                        {"role": "tool", "tool_call_id": "call_1", "content": large_tool_output},
                        {"role": "tool", "tool_call_id": "call_json", "content": '{"status":"ok","items":[1,2,3]}'},
                        {"role": "assistant", "content": "```python\nprint('same')\nprint('same')\n```"},
                    ],
                    "tools": [{"type": "function", "function": {"name": "run", "description": "same"}}],
                },
            },
        }

    def test_raw_and_off_are_identical_and_safe_preserves_structure(self):
        report = evaluate.evaluate([StringIO(json.dumps(self._record()) + "\n")], evaluate.PolicyConfig())
        sample = report["samples"][0]
        self.assertEqual(sample["modes"]["raw"]["request_bytes"], sample["modes"]["off"]["request_bytes"])
        self.assertEqual(sample["modes"]["off"]["bytes_saved_vs_raw"], 0)
        self.assertGreater(sample["modes"]["safe"]["bytes_saved_vs_raw"], 0)
        self.assertTrue(sample["modes"]["safe"]["invariants_passed"])
        self.assertEqual(sample["modes"]["safe"]["rule_hits"]["repeated_groups"], 0)
        self.assertEqual(
            sample["modes"]["raw"]["estimated_input_tokens_bytes_div_4"],
            sample["modes"]["raw"]["request_bytes"] / 4.0,
        )
        self.assertEqual(report["summary"]["records_failed"], 0)

    def test_bounded_recomputes_tool_counts_from_body_and_saves_tool_bytes(self):
        record = self._record()
        report = evaluate.evaluate([StringIO(json.dumps(record) + "\n")], evaluate.PolicyConfig(max_tool_result_bytes=512, head_bytes=128, tail_bytes=128))
        sample = report["samples"][0]
        bounded = sample["modes"]["bounded"]
        self.assertGreater(bounded["tool_result_bytes_saved_vs_raw"], 0)
        self.assertGreater(bounded["rule_hits"]["oversized_tool_results"], 0)
        self.assertTrue(bounded["invariants"]["tool_call_structure_unchanged"])
        self.assertTrue(bounded["invariants"]["structural_fields_unchanged"])
        self.assertTrue(bounded["invariants"]["system_unchanged"])
        self.assertTrue(bounded["invariants"]["structured_json_unchanged"])

    def test_code_tool_arguments_and_json_are_not_rewritten(self):
        record = self._record()
        body = record["replay"]["body"]
        report = evaluate.evaluate([StringIO(json.dumps(record) + "\n")], evaluate.PolicyConfig(max_tool_result_bytes=256, head_bytes=64, tail_bytes=64))
        sample = report["samples"][0]
        self.assertTrue(sample["modes"]["bounded"]["invariants"]["code_unchanged"])
        self.assertTrue(sample["modes"]["bounded"]["invariants"]["tool_call_structure_unchanged"])
        self.assertTrue(sample["modes"]["bounded"]["invariants"]["structural_fields_unchanged"])
        self.assertTrue(sample["modes"]["bounded"]["invariants"]["structured_json_unchanged"])
        self.assertEqual(body["messages"][2]["tool_calls"][0]["function"]["arguments"], '{"command":"echo same\\nsame"}')

    def test_never_worse_for_short_repeated_line(self):
        record = self._record()
        record["replay"]["body"]["messages"][1]["content"] = "x\nx"
        report = evaluate.evaluate([StringIO(json.dumps(record) + "\n")], evaluate.PolicyConfig())
        mode = report["samples"][0]["modes"]["safe"]
        self.assertGreaterEqual(mode["bytes_saved_vs_raw"], 0)

    def test_report_contains_no_request_body(self):
        report = evaluate.evaluate([StringIO(json.dumps(self._record()) + "\n")], evaluate.PolicyConfig())
        serialized = json.dumps(report, ensure_ascii=False)
        self.assertNotIn("tool line", serialized)
        self.assertNotIn("top level system marker", serialized)
        self.assertIn("ctx-0000000000000001", serialized)


if __name__ == "__main__":
    unittest.main()
