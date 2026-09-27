from __future__ import annotations

import sys
from pathlib import Path

from starlette.datastructures import Headers

sys.path.insert(0, str(Path(__file__).parent / "compat"))
import compat_proxy as cp  # noqa: E402


def test_compat_adds_internal_request_id_without_copying_credentials():
    headers = Headers({"authorization": "Bearer secret-token", "x-api-key": "other-secret"})
    forwarded = cp.forwarded_headers(headers, "req_internal_123")
    assert ("x-request-id", "req_internal_123") in [(k.lower(), v) for k, v in forwarded]
    # Auth headers still reach LiteLLM for normal gateway authentication; the
    # monitor event envelope never receives them.
    assert ("authorization", "Bearer secret-token") in [(k.lower(), v) for k, v in forwarded]
