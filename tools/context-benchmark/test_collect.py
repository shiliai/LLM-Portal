from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
import collect  # noqa: E402


class FakeOpf:
    info = {"health": {"ready": True, "version": "fixture"}}

    def redact(self, texts):
        return [text.replace("CONTACT_TOKEN_A", "<PRIVATE_CONTACT>").replace("CONTACT_TOKEN_B", "<PRIVATE_CONTACT>") for text in texts]


class CollectTest(unittest.TestCase):
    def test_http_fixture_drops_headers_and_sensitive_fields(self):
        record = {
            "metadata": {"client": "fixture", "request_id": "must-not-survive", "client_ip": "192.0.2.1"},
            "request": {
                "url": "https://internal.example/v1/chat/completions",
                "headers": {"authorization": "Bearer secret"},
                "body": {"model": "fixture", "messages": [{"role": "user", "content": "CONTACT_TOKEN_A"}]},
            },
        }
        result = collect.collect_record(record, FakeOpf(), 1, False)
        text = json.dumps(result, ensure_ascii=False)
        self.assertNotIn("CONTACT_TOKEN_A", text)
        self.assertNotIn("Bearer secret", text)
        self.assertNotIn("internal.example", text)
        self.assertEqual(result["metadata"]["endpoint"], "/v1/chat/completions")
        self.assertEqual(result["aggregate"]["redacted_slot_count"], 1)
        self.assertEqual(result["replay"]["body"]["messages"][0]["content"], "<PRIVATE_CONTACT>")

    def test_nested_tool_schema_user_and_metadata_keys_are_preserved(self):
        body = {
            "model": "fixture",
            "user": "top-level-user-must-not-survive",
            "metadata": {"trace_id": "top-level-trace-must-not-survive"},
            "tools": [{"type": "function", "function": {
                "name": "lookup",
                "parameters": {"type": "object", "properties": {
                    "user": {"type": "string", "description": "synthetic user label"},
                    "metadata": {"type": "object", "properties": {"user": {"type": "string"}}},
                }},
            }}],
            "messages": [{"role": "user", "content": "call lookup"}],
        }
        result = collect.collect_record({"body": body}, FakeOpf(), 4, False)
        self.assertNotIn("user", result["replay"]["body"])
        self.assertNotIn("metadata", result["replay"]["body"])
        properties = result["replay"]["body"]["tools"][0]["function"]["parameters"]["properties"]
        self.assertIn("user", properties)
        self.assertIn("metadata", properties)
        self.assertIn("user", properties["metadata"]["properties"])

    def test_anthropic_tool_result_is_counted_and_image_is_omitted(self):
        body = {
            "model": "fixture",
            "messages": [{"role": "user", "content": [
                {"type": "image", "source": {"type": "base64", "data": "not-a-real-image"}},
                {"type": "tool_result", "tool_use_id": "toolu_1", "content": "CONTACT_TOKEN_B"},
            ]}],
        }
        result = collect.collect_record({"endpoint": "/v1/messages", "body": body}, FakeOpf(), 2, False)
        self.assertEqual(result["protocol"], "anthropic_messages")
        self.assertEqual(result["aggregate"]["tool_result_count"], 1)
        self.assertEqual(result["aggregate"]["image_blocks"], 1)
        image = result["replay"]["body"]["messages"][0]["content"][0]
        self.assertEqual(image["source"]["data"], "[IMAGE_OMITTED]")
        self.assertNotIn("CONTACT_TOKEN_B", json.dumps(result))

    def test_size_guard_and_assumed_redacted_mode(self):
        client = collect.OpfClient("http://fixture.invalid")
        with self.assertRaises(collect.CollectionError):
            client.redact(["x" * (collect.MAX_TEXT_BYTES + 1)])
        record = {"body": {"model": "fixture", "messages": [{"role": "user", "content": "synthetic"}]}}
        result = collect.collect_record(record, None, 3, True)
        self.assertEqual(result["redaction"]["status"], "assumed_input_redacted")


if __name__ == "__main__":
    unittest.main()
