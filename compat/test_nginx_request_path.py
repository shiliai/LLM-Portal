#!/usr/bin/env python3
"""Nginx request-ID and streaming invariants for the public gateway templates."""
from __future__ import annotations

from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
TEMPLATES = (
    ROOT / "vps/nginx/private-llm.conf",
    ROOT / "vps/nginx/private-llm-offload.conf",
)
REQUEST_ID_HEADER = "proxy_set_header X-Request-Id $request_id;"
RESPONSE_ID_HEADER = "add_header X-Request-Id $request_id always;"


class NginxRequestPathTemplateTest(unittest.TestCase):
    def test_request_id_is_forwarded_and_echoed_for_all_public_paths(self):
        for template in TEMPLATES:
            with self.subTest(template=template.name):
                config = template.read_text()
                # A server-level `always` response header covers proxy responses, redirects,
                # ACME, and the intentional public 404 boundary.
                self.assertEqual(config.count(RESPONSE_ID_HEADER), config.count("server {"))
                # proxy_set_header does not inherit into a location that has other proxy headers.
                self.assertEqual(config.count(REQUEST_ID_HEADER), config.count("proxy_pass "))
                self.assertEqual(config.count("proxy_hide_header X-Request-Id;"), 1)

    def test_log_format_is_an_http_fragment_directive(self):
        for template in TEMPLATES:
            with self.subTest(template=template.name):
                config = template.read_text()
                self.assertEqual(config.count("log_format private_llm_rid"), 1)
                self.assertLess(config.index("log_format private_llm_rid"), config.index("server {"))
                self.assertIn("access_log /var/log/nginx/private-llm.access.log private_llm_rid;", config)

    def test_sse_locations_remain_unbuffered(self):
        for template in TEMPLATES:
            with self.subTest(template=template.name):
                config = template.read_text()
                self.assertGreaterEqual(config.count("proxy_buffering off;"), 5)
                self.assertIn("proxy_read_timeout 3600s;", config)


if __name__ == "__main__":
    unittest.main()
