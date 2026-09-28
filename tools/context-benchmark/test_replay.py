from __future__ import annotations

import json
import stat
import tempfile
import unittest
from io import StringIO
from pathlib import Path
from unittest.mock import patch

import replay


class _StreamResponse:
    status = 200

    def __init__(self, lines: list[str]):
        self._lines = iter(lines)

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def getcode(self):
        return self.status

    def readline(self):
        try:
            return next(self._lines).encode("utf-8")
        except StopIteration:
            return b""


class ReplayTest(unittest.TestCase):
    def _body(self, content: str = "line\nline\nline\n"):
        return {
            "model": "fixture",
            "stream": True,
            "messages": [
                {"role": "assistant", "tool_calls": [{"id": "call-1", "type": "function"}]},
                {"role": "tool", "tool_call_id": "call-1", "content": content},
            ],
        }

    def test_safe_candidate_keeps_structure_and_can_shrink(self):
        body = self._body("same\n" * 10)
        candidate = replay.make_candidate(body, "safe", replay.PolicyConfig())
        self.assertLess(candidate.request_bytes, candidate.raw_request_bytes)
        self.assertEqual(candidate.body["messages"][0]["tool_calls"][0]["id"], "call-1")
        self.assertEqual(candidate.body["messages"][1]["tool_call_id"], "call-1")
        self.assertTrue(candidate.changed)

    def test_evenly_spaced_selection_covers_dataset_edges(self):
        records = [(line, {"line": line}) for line in range(1, 11)]
        selected = replay._select_records(records, 4, "evenly_spaced")
        self.assertEqual([line for line, _ in selected], [1, 4, 7, 10])

    def test_rtk_uses_pipe_stdin_and_caches_duplicate_text(self):
        with tempfile.TemporaryDirectory() as tmp:
            executable = Path(tmp) / "fake-rtk"
            executable.write_text(
                "#!/usr/bin/env python3\n"
                "import sys\n"
                "assert sys.argv[1:] == ['--skip-env', 'pipe']\n"
                "sys.stdout.write('short')\n",
                encoding="utf-8",
            )
            executable.chmod(executable.stat().st_mode | stat.S_IXUSR)
            filt = replay.RtkFilter(str(executable), 2)
            body = self._body("same tool output")
            body["messages"].append({"role": "tool", "tool_call_id": "call-2", "content": "same tool output"})
            candidate = replay.make_candidate(body, "rtk", replay.PolicyConfig(), filt)
            self.assertEqual(filt.calls, 1)
            self.assertGreaterEqual(filt.cache_hits, 1)
            self.assertLess(candidate.request_bytes, candidate.raw_request_bytes)

    def test_stream_result_records_protocol_fields_without_response_text(self):
        response = _StreamResponse([
            "data: {\"choices\":[{\"delta\":{\"role\":\"assistant\"}}]}\n",
            "data: {\"choices\":[{\"delta\":{\"content\":\"ok\"},\"finish_reason\":\"stop\"}],\"usage\":{\"prompt_tokens\":12}}\n",
            "data: [DONE]\n",
        ])
        with patch("replay.urllib.request.urlopen", return_value=response):
            result = replay.send_request("http://example.test/v1/chat/completions", self._body(), "", 2)
        self.assertEqual(result["status"], "ok")
        self.assertTrue(result["sse_complete"])
        self.assertEqual(result["usage"]["prompt_tokens"], 12)
        self.assertNotIn("response", result)

    def test_replay_report_does_not_contain_response_body(self):
        record = {"sample_id": "sample-1", "protocol": "openai_chat", "replay": {"endpoint": "/v1/chat/completions", "body": self._body()}}
        response = _StreamResponse(["data: {\"choices\":[{\"delta\":{\"content\":\"private response\"}}]}\n", "data: [DONE]\n"])
        with patch("replay.urllib.request.urlopen", return_value=response):
            report = replay.replay([StringIO(json.dumps(record) + "\n")], "http://example.test", "", ["off"], replay.PolicyConfig(), 2, 0, None)
        encoded = json.dumps(report, ensure_ascii=False)
        self.assertNotIn("private response", encoded)
        self.assertEqual(report["inputs"]["records_loaded"], 1)

    def test_invalid_image_payload_is_skipped_before_http(self):
        body = self._body()
        body["messages"].insert(0, {
            "role": "user",
            "content": [{"type": "image_url", "image_url": {"url": "data:image/png;base64,broken"}}],
        })
        record = {"sample_id": "sample-image", "protocol": "openai_chat", "replay": {"body": body}}
        with patch("replay.urllib.request.urlopen") as request:
            report = replay.replay([StringIO(json.dumps(record) + "\n")], "http://example.test", "", ["off"], replay.PolicyConfig(), 2, 0, None)
        request.assert_not_called()
        attempt = report["attempts"][0]
        self.assertEqual(attempt["status"], "skipped")
        self.assertEqual(attempt["error_kind"], "invalid_image_payload")


if __name__ == "__main__":
    unittest.main()
