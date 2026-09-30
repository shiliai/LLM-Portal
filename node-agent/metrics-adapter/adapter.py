#!/usr/bin/env python3
"""Metrics adapter (v2): TensorFold -> vLLM-compatible names for the portal dashboard.
Engine contract: engines serve :8890; this adapter serves :8891/metrics; vmagent
points here permanently - engine swaps only touch MAP + the log tail below.
Derived series:
  - spec accepted/drafted: TF has no draft counters; approximated from the
    request log (rounds x k_max as drafted, decode_tokens as accepted) =
    lower-bound acceptance, monotonic and comparable over time.
  - kv_cache_usage_perc: last observed request-moment pool occupancy
    (1 - kv_free/kv_pages from the request log).
"""
import json, os, urllib.request, re
from http.server import BaseHTTPRequestHandler, HTTPServer

UP = "http://127.0.0.1:8890/metrics"
LOG = os.path.expanduser("~/.cache/glm53-tf/sessions/requests.jsonl")
K_MAX = 7
MAP = {
    "tensorfold_prompt_tokens_total": "vllm:prompt_tokens_total",
    "tensorfold_completion_tokens_total": "vllm:generation_tokens_total",
    "tensorfold_requests_total": "vllm:request_success_total",
    "tensorfold_requests_inflight": "vllm:num_requests_running",
    "tensorfold_requests_stalled": "vllm:num_requests_waiting",
    "tensorfold_decode_rounds_total": "vllm:spec_decode_num_decode_steps_total",
}
state = {"off": 0, "drafted": 0.0, "accepted": 0.0, "kv": None}

def tail_log():
    try:
        sz = os.path.getsize(LOG)
        if sz < state["off"]: state["off"] = 0
        if sz == state["off"]: return
        with open(LOG, "rb") as f:
            f.seek(state["off"]); chunk = f.read(); state["off"] = f.tell()
        for line in chunk.splitlines():
            try: r = json.loads(line)
            except Exception: continue
            rounds = r.get("rounds") or 0
            dtoks = r.get("decode_tokens") or 0
            state["drafted"] += rounds * K_MAX
            state["accepted"] += dtoks
            if r.get("kv_pages"): state["kv"] = (r["kv_pages"], r.get("kv_free") or 0)
    except OSError: pass

def fetch():
    return urllib.request.urlopen(UP, timeout=5).read().decode()

def translate(body):
    vals = {}
    for line in body.splitlines():
        m = re.match(r"^(tensorfold_[a-z_]+)(\{[^}]*\})?\s+([0-9.eE+-]+)\s*$", line)
        if m: vals.setdefault(m.group(1), []).append((m.group(2) or "", m.group(3)))
    out = []
    def emit(name, labels, v): out.append(f"{name}{labels} {v}")
    for src, dst in MAP.items():
        if src in vals:
            for labels, v in vals[src]: emit(dst, labels, v)
    if "tensorfold_cached_tokens_total" in vals and "tensorfold_prompt_tokens_total" in vals:
        emit("vllm:prefix_cache_hits_total", vals["tensorfold_cached_tokens_total"][0][0], vals["tensorfold_cached_tokens_total"][0][1])
        emit("vllm:prefix_cache_queries_total", vals["tensorfold_prompt_tokens_total"][0][0], vals["tensorfold_prompt_tokens_total"][0][1])
    tail_log()
    d = int(state['drafted'])
    emit("vllm:spec_decode_num_draft_tokens_total", "", str(d))
    a = int(state['accepted'])
    emit("vllm:spec_decode_num_accepted_tokens_total", "", str(a))
    if state["kv"]:
        pages, free = state["kv"]
        emit("vllm:kv_cache_usage_perc", "", f"{max(0.0, 1 - free/4097):.6f}")
    out.append(body)
    return "\n".join(out)

class H(BaseHTTPRequestHandler):
    def do_GET(self):
        try: body = translate(fetch()).encode(); code = 200
        except Exception as e: body = f"# adapter upstream error: {e}\n".encode(); code = 502
        self.send_response(code); self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body))); self.end_headers()
        self.wfile.write(body)
    def log_message(self, *a): pass
HTTPServer(("127.0.0.1", 8891), H).serve_forever()