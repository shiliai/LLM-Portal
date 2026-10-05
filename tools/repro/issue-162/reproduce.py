#!/usr/bin/env python3
"""Issue #162 的独立 HTTP 复现器；仅发送推理请求，不执行样本中的命令。"""
from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import json
import os
from pathlib import Path
import re
import struct
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

ROOT = Path(__file__).resolve().parent
MOJIBAKE = re.compile(r"[\u00c2-\u00f4][\u0080-\u00bf]{1,3}")


def token_hash(tokens: list[int]) -> str:
    return hashlib.sha256(struct.pack("<" + str(len(tokens)) + "I", *tokens)).hexdigest()


def load_case() -> tuple[dict, dict, str]:
    raw = (ROOT / "request.json").read_bytes()
    manifest = json.loads((ROOT / "manifest.json").read_text(encoding="utf-8"))
    digest = hashlib.sha256(raw).hexdigest()
    if digest != manifest["request_file_sha256"]:
        raise ValueError("request.json 校验失败；修改后的样本应作为另一份实验记录")
    case = json.loads(raw)
    prompt = case["prompt"]
    if prompt != (ROOT / "prompt.txt").read_text(encoding="utf-8"):
        raise ValueError("prompt.txt 与请求中的实际输入不一致")
    if MOJIBAKE.search(prompt) or any(128 <= ord(c) <= 159 for c in prompt) or "\ufffd" in prompt:
        raise ValueError("输入已经包含乱码特征，不能用于证明本次生成引入乱码")
    return case, manifest, digest


def sse_events(response):
    """在完整 SSE frame 上解析 JSON，避免按网络 chunk 错拆 UTF-8。"""
    data_lines: list[bytes] = []
    for raw in response:
        line = raw.rstrip(b"\r\n")
        if not line:
            if not data_lines:
                continue
            payload = b"\n".join(data_lines)
            data_lines.clear()
            if payload.strip() == b"[DONE]":
                yield None
                return
            yield json.loads(payload)
        elif line.startswith(b"data:"):
            data_lines.append(line[5:].lstrip(b" "))


def run_once(index: int, case: dict, manifest: dict, args) -> dict:
    body = dict(case)
    body["temperature"] = args.temperature
    body["stream"] = args.stream
    if args.model:
        body["model"] = args.model
    if args.no_draft:
        body["draft"] = False
    if args.stream:
        body["stream_options"] = {"include_usage": True}
    headers = {"Content-Type": "application/json"}
    key = os.environ.get("TENSORFOLD_API_KEY")
    if key:
        headers["Authorization"] = "Bearer " + key
    request = urllib.request.Request(
        args.base_url.rstrip("/") + "/completions",
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"), headers=headers,
    )
    text = ""
    usage = {}
    stats = {}
    finish_reasons = []
    got_done = not args.stream
    started = time.monotonic()
    try:
        with urllib.request.urlopen(request, timeout=args.timeout) as response:
            events = sse_events(response) if args.stream else [json.load(response)]
            for event in events:
                if event is None:
                    got_done = True
                    break
                if event.get("error"):
                    raise ValueError("服务端返回错误事件")
                if event.get("usage"):
                    usage = event["usage"]
                if event.get("tensorfold"):
                    stats = event["tensorfold"]
                for choice in event.get("choices", []):
                    value = choice.get("text")
                    if isinstance(value, str):
                        text += value
                    if choice.get("finish_reason"):
                        finish_reasons.append(choice["finish_reason"])
        if not got_done:
            raise ValueError("SSE 未收到 [DONE]，本次结果不完整")
        if not text:
            raise ValueError("响应没有生成文本，本次无法判断")
        tokens = stats.get("token_ids")
        matches = list(MOJIBAKE.finditer(text))
        c1 = sum(128 <= ord(c) <= 159 for c in text)
        return {
            "run": index, "status": "ok", "seconds": round(time.monotonic() - started, 3),
            "temperature": args.temperature, "stream": args.stream, "draft": not args.no_draft,
            "text": text, "codepoints": [f"U+{ord(c):04X}" for c in text],
            "mojibake_sequences": len(matches), "C1_controls": c1,
            "replacement_characters": text.count("\ufffd"),
            "reproduced": bool(matches or c1), "usage": usage,
            "input_tokens_match_fixture": usage.get("prompt_tokens") == manifest["input_tokens"],
            "finish_reasons": finish_reasons, "token_ids": tokens,
            "token_sha256": token_hash(tokens) if isinstance(tokens, list) else None,
            "matches_recorded_greedy_tokens": tokens == manifest["reference_greedy_token_ids"],
        }
    except urllib.error.HTTPError as exc:
        # 鉴权错误或网关错误体可能带环境信息，只报告类型和状态码。
        return {"run": index, "status": "error", "error_type": "HTTPError", "http_status": exc.code}
    except (OSError, ValueError, KeyError, TypeError) as exc:
        return {"run": index, "status": "error", "error_type": type(exc).__name__}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8890/v1")
    parser.add_argument("--model", help="服务端实际公开的模型名；默认沿用请求文件")
    parser.add_argument("--temperature", type=float, choices=(0.0, 1.0), default=0.0)
    parser.add_argument("--stream", action="store_true")
    parser.add_argument("--no-draft", action="store_true")
    parser.add_argument("--repeat", type=int, default=1)
    parser.add_argument("--concurrency", type=int, choices=(1, 2, 3, 4), default=1)
    parser.add_argument("--timeout", type=float, default=180)
    parser.add_argument("--check", action="store_true", help="仅检查本地样本，不连接服务")
    args = parser.parse_args()
    parsed = urllib.parse.urlsplit(args.base_url)
    if (parsed.scheme not in ("http", "https") or not parsed.hostname
            or parsed.username or parsed.password or parsed.query or parsed.fragment):
        parser.error("base-url 应为不带凭据、查询参数或片段的 HTTP(S) API 地址")
    if args.repeat < 1 or args.timeout <= 0:
        parser.error("repeat 和 timeout 必须大于 0")
    try:
        case, manifest, digest = load_case()
    except (OSError, ValueError, KeyError) as exc:
        print(json.dumps({"status": "fixture_error", "error_type": type(exc).__name__}))
        return 2
    if args.check:
        print(json.dumps({"status": "verified", "request_file_sha256": digest,
                          "prompt_characters": len(case["prompt"]),
                          "recorded_input_tokens": manifest["input_tokens"]}, indent=2))
        return 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.concurrency) as pool:
        results = list(pool.map(lambda i: run_once(i, case, manifest, args), range(args.repeat)))
    print(json.dumps({"fixture_sha256": digest, "results": results}, ensure_ascii=True, indent=2))
    if any(r["status"] != "ok" or not r["input_tokens_match_fixture"] for r in results):
        return 2
    return 1 if any(r["reproduced"] for r in results) else 0


if __name__ == "__main__":
    sys.exit(main())
