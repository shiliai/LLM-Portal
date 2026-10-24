#!/usr/bin/env python3
"""离线生成单 deployment endpoint 增量计划，并验证其他路由字段守恒。"""
from __future__ import annotations

import argparse
import copy
import json
import os
from pathlib import Path
from urllib.parse import quote, urlsplit


ACK = "MAPPING_PLAN_WRITE_AUTHORIZED"


def deployments(snapshot: dict) -> dict:
    rows = snapshot.get("data")
    if not isinstance(rows, list) or not rows:
        raise ValueError("expected nonempty /model/info data list")
    result = {}
    for row in rows:
        ident = (row.get("model_info") or {}).get("id")
        if not isinstance(ident, str) or not ident or ident in result:
            raise ValueError("missing or duplicate deployment id")
        result[ident] = row
    return result


def endpoint(value: str) -> str:
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or \
            parsed.username is not None or parsed.password is not None or \
            parsed.query or parsed.fragment or parsed.path.rstrip("/") != "/v1":
        raise ValueError("endpoint must be an HTTP(S) /v1 URL without credentials or query")
    parsed.port  # 校验非法端口。
    return value.rstrip("/")


def make_plan(before: dict, ident: str, model: str, old: str, new: str) -> dict:
    rows = deployments(before)
    row = rows[ident]
    params = row.get("litellm_params") or {}
    old, new = endpoint(old), endpoint(new)
    if row.get("model_name") != model or params.get("api_base", "").rstrip("/") != old:
        raise ValueError("deployment precondition mismatch")
    if old == new:
        raise ValueError("endpoint is unchanged")
    # 只输出单字段差异，绝不复制 model_info、预算或可能已被掩码的 api_key。
    return {"version": 1, "deployment_id": ident, "model_name": model,
            "expected_api_base": params["api_base"],
            "changes": {"api_base": new},
            "rollback": {"api_base": params["api_base"]},
            "http_method": "PATCH", "path": f"/model/{quote(ident, safe='')}/update",
            "body": {"litellm_params": {"api_base": new}}}


def comparable(rows: dict) -> dict:
    rows = copy.deepcopy(rows)
    for row in rows.values():
        for values in (row, row.get("model_info") or {}):
            for key in ("updated_at", "updated_by"):
                values.pop(key, None)
    return rows


def verify(before: dict, after: dict, plan: dict) -> None:
    if plan.get("version") != 1 or set(plan.get("changes", {})) != {"api_base"}:
        raise ValueError("invalid endpoint-only plan")
    original = deployments(before)
    actual = deployments(after)
    ident = plan["deployment_id"]
    row = original[ident]
    if row.get("model_name") != plan["model_name"] or \
            row["litellm_params"]["api_base"] != plan["expected_api_base"]:
        raise ValueError("plan does not match original deployment")
    expected = copy.deepcopy(original)
    expected[ident]["litellm_params"]["api_base"] = endpoint(plan["changes"]["api_base"])
    if comparable(actual) != comparable(expected):
        raise ValueError("deployment fields or set changed beyond the selected endpoint")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", nargs="?", default="check", choices=("check", "verify"))
    parser.add_argument("--before", type=Path, required=True)
    parser.add_argument("--after", type=Path)
    parser.add_argument("--deployment-id")
    parser.add_argument("--model")
    parser.add_argument("--old-api-base")
    parser.add_argument("--new-api-base")
    parser.add_argument("--plan", type=Path)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--ack", default="")
    args = parser.parse_args(argv)
    try:
        before = json.loads(args.before.read_text())
        if args.action == "verify":
            verify(before, json.loads(args.after.read_text()), json.loads(args.plan.read_text()))
        else:
            plan = make_plan(before, args.deployment_id, args.model,
                             args.old_api_base, args.new_api_base)
            if args.apply:
                if args.ack != ACK or args.plan is None:
                    raise ValueError("plan write requires explicit acknowledgement and path")
                with args.plan.open("x", encoding="utf-8") as stream:
                    os.chmod(args.plan, 0o600)
                    json.dump(plan, stream, indent=2)
                    stream.write("\n")
                    stream.flush()
                    os.fsync(stream.fileno())
        print(json.dumps({"status": "verified" if args.action == "verify" else "checked",
                          "deployments": len(deployments(before)), "live_api_write": False}))
        return 0
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        print(json.dumps({"status": "failed", "live_api_write": False}),
              file=__import__("sys").stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
