from __future__ import annotations

import json
import os
import stat
import sys
import tempfile
import unittest
from io import StringIO
from pathlib import Path


ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
import rtk_benchmark  # noqa: E402


class RtkBenchmarkTest(unittest.TestCase):
    def _record(self, content: str) -> dict:
        return {
            "sample_id": "redacted-1",
            "replay": {
                "body": {
                    "messages": [
                        {"role": "assistant", "tool_calls": [{"id": "call-1"}]},
                        {"role": "tool", "tool_call_id": "call-1", "content": content},
                    ]
                }
            },
        }

    def test_missing_executable_is_blocked_and_does_not_claim_rtk(self):
        line = json.dumps(self._record("captured output")) + "\n"
        report = rtk_benchmark.benchmark([StringIO(line)], None, 1.0, "c75f159")
        self.assertEqual(report["execution"], "blocked")
        self.assertEqual(report["summary"]["tool_results"], 0)
        self.assertEqual(report["failure_reasons"], {"missing_executable": 1})

    def test_only_pipe_stdin_is_used_and_commands_are_not_executed(self):
        with tempfile.TemporaryDirectory() as tmp:
            executable = Path(tmp) / "fake-rtk"
            executable.write_text(
                "#!/usr/bin/env python3\n"
                "import sys\n"
                "assert sys.argv[1:] == ['--skip-env', 'pipe']\n"
                "data = sys.stdin.read()\n"
                "print('filtered:' + str(len(data.encode('utf-8'))))\n",
                encoding="utf-8",
            )
            executable.chmod(executable.stat().st_mode | stat.S_IXUSR)
            repeated = "git status; touch SHOULD_NEVER_EXIST\n" * 2
            line = json.dumps(self._record(repeated)) + "\n"
            report = rtk_benchmark.benchmark([StringIO(line)], str(executable), 1.0, "c75f159")

        summary = report["summary"]
        self.assertEqual(report["execution"], "actual_rtk")
        self.assertEqual(summary["tool_results"], 1)
        self.assertEqual(summary["unique_tool_results"], 1)
        self.assertEqual(summary["rtk_successes"], 1)
        self.assertEqual(summary["rtk_failures"], 0)
        self.assertGreater(summary["bytes_saved"], 0)

    def test_identical_tool_results_are_cached_but_counted(self):
        with tempfile.TemporaryDirectory() as tmp:
            executable = Path(tmp) / "fake-rtk"
            executable.write_text(
                "#!/usr/bin/env python3\n"
                "import sys\n"
                "sys.stdout.write(sys.stdin.read()[:1])\n",
                encoding="utf-8",
            )
            executable.chmod(executable.stat().st_mode | stat.S_IXUSR)
            record = self._record("same tool output")
            record["replay"]["body"]["messages"].append(
                {"role": "tool", "tool_call_id": "call-2", "content": "same tool output"}
            )
            report = rtk_benchmark.benchmark(
                [StringIO(json.dumps(record) + "\n")], str(executable), 1.0, "c75f159"
            )

        summary = report["summary"]
        self.assertEqual(summary["tool_results"], 2)
        self.assertEqual(summary["unique_tool_results"], 1)
        self.assertEqual(summary["rtk_successes"], 2)


if __name__ == "__main__":
    unittest.main()
