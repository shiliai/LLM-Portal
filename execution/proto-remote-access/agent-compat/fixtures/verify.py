#!/usr/bin/env python3
import hashlib
import json
import sys
from pathlib import Path


def fail(code: str, hint: str) -> None:
    print(json.dumps({"status": "REJECTED", "code": code, "hint": hint}))
    raise SystemExit(1)


def main() -> None:
    if len(sys.argv) != 2:
        fail("usage", "pass one JSON result path")
    path = Path(sys.argv[1])
    try:
        value = json.loads(path.read_text())
    except Exception as exc:
        fail("invalid_json", str(exc))

    if path.name == "result.json":
        invalid = [item for item in value.get("selected_ids", []) if int(item[1:]) % 3 == 0]
        if invalid:
            fail(
                "integrity_constraint",
                f"remove IDs whose numeric part is divisible by 3: {','.join(invalid)}",
            )
        expected_ids = ["A17", "B04"]
        expected_total = 176.65
        signature_source = ",".join(expected_ids)
        expected_signature = hashlib.sha256(signature_source.encode()).hexdigest()[:12]
        expected = {
            "selected_ids": expected_ids,
            "grand_total": expected_total,
            "signature": expected_signature,
        }
    elif path.name == "final_summary.json":
        expected = {
            "selected_ids": ["A17", "B04"],
            "prior_total": 176.65,
            "adjustment": 3.35,
            "adjusted_total": 180.00,
        }
    else:
        fail("unexpected_file", "verify result.json or final_summary.json")

    if value != expected:
        fail("mismatch", f"expected exactly {json.dumps(expected, sort_keys=True)}")
    print("VERIFIED")


if __name__ == "__main__":
    main()
