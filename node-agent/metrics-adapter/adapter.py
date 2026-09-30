#!/usr/bin/env python3
"""Metrics adapter (v3): TensorFold -> dashboard contract.

Engine contract: engines serve raw /metrics on :8890; this adapter serves
:8891/metrics; vmagent points here permanently. Engine swaps touch only MAP
and the request-log fields below.

v3: a 1 s sampler thread polls the engine between vmagent scrapes and emits
llamacpp-style interval gauges (the console prefers them over irate):
  llamacpp:predicted_tokens_seconds  output tok/s, interval average
  llamacpp:prompt_tokens_seconds     computed-prefill tok/s (cached hits excluded)
and reports running requests as the max inflight within the window, so short
tasks are no longer invisible behind the 15 s scrape + write lag.
"""
import json, os, threading, time, urllib.request, re
from http.server import BaseHTTPRequestHandler, HTTPServer

UP = "http://127.0.0.1:8890/metrics"
LOG = os.path.expanduser("~/.cache/glm53-tf/sessions/requests.jsonl")
K_MAX = 7
WINDOW = 10.0
POOL_PAGES = 4097
MAP = {
    "tensorfold_prompt_tokens_total": "vllm:prompt_tokens_total",
    "tensorfold_cached_tokens_total": "vllm:prefix_cache_hits_total",
    "tensorfold_completion_tokens_total": "vllm:generation_tokens_total",
    "tensorfold_requests_total": "vllm:request_success_total",
    "tensorfold_requests_stalled": "vllm:num_requests_waiting",
    "tensorfold_decode_rounds_total": "vllm:spec_decode_num_decode_steps_total",
}
state = {"off": 0, "drafted": 0.0, "accepted": 0.0, "kv": None, "ema_tps": 0.0,
         "samples": [], "lock": threading.Lock()}
_gauges = {"out_tps": 0.0, "in_tps": 0.0, "running_max": 0}

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
            state["drafted"] += (r.get("rounds") or 0) * K_MAX
            state["accepted"] += (r.get("decode_tokens") or 0)
            t = r.get("decode_tps")
            if t:
                state["ema_tps"] = t if not state["ema_tps"] else 0.7 * state["ema_tps"] + 0.3 * t
            if r.get("kv_pages"): state["kv"] = (r["kv_pages"], r.get("kv_free") or 0)
    except OSError: pass

def sample():
    body = urllib.request.urlopen(UP, timeout=4).read().decode()
    vals = {}
    for line in body.splitlines():
        m = re.match(r"^tensorfold_([a-z_]+)(\{[^}]*\})?\s+([0-9.eE+-]+)\s*$", line)
        if m: vals[m.group(1)] = float(m.group(3))
    now = time.time()
    with state["lock"]:
        s = state["samples"]
        s.append((now, vals.get("completion_tokens_total", 0.0),
                  vals.get("prompt_tokens_total", 0.0),
                  vals.get("cached_tokens_total", 0.0),
                  vals.get("requests_inflight", 0.0)))
        while s and now - s[0][0] > WINDOW + 5: s.pop(0)
        if len(s) >= 2:
            t0, g0, p0, c0, _ = s[0]; t1, g1, p1, c1, _ = s[-1]
            dt = max(t1 - t0, 1e-6)
            _gauges["out_tps"] = max(0.0, (g1 - g0) / dt)
            if _gauges["running_max"] and state["ema_tps"]:
                _gauges["out_tps"] = _gauges["running_max"] * state["ema_tps"]
            _gauges["in_tps"] = max(0.0, ((p1 - c1) - (p0 - c0)) / dt)
        _gauges["running_max"] = max(v[4] for v in s) if s else 0
    tail_log()

def sampler():
    while True:
        try: sample()
        except Exception: pass
        time.sleep(1.0)

def fetch():
    return urllib.request.urlopen(UP, timeout=5).read().decode()

def translate(body):
    out = []
    for line in body.splitlines():
        m = re.match(r"^(tensorfold_[a-z_]+)(\{[^}]*\})?\s+([0-9.eE+-]+)\s*$", line)
        if not m: continue
        name = "tensorfold_" + m.group(1)
        dst = MAP.get(name)
        if dst: out.append(f"{dst}{m.group(2) or ''} {m.group(3)}")
        if name == "tensorfold_cached_tokens_total":
            out.append(f"vllm:cache_cached_prompt_tokens_total{m.group(2) or ''} {m.group(3)}")
    with state["lock"]:
        out.append("llamacpp:predicted_tokens_seconds %.2f" % _gauges["out_tps"])
        out.append("llamacpp:prompt_tokens_seconds %.2f" % _gauges["in_tps"])
        out.append("vllm:num_requests_running %.0f" % _gauges["running_max"])
        d, a = int(state["drafted"]), int(state["accepted"])
    out.append("vllm:spec_decode_num_draft_tokens_total %d" % d)
    out.append("vllm:spec_decode_num_accepted_tokens_total %d" % a)
    if state["kv"]:
        pages, free = state["kv"]
        out.append("vllm:kv_cache_usage_perc %.6f" % max(0.0, 1 - free / POOL_PAGES))
    out.append(body)
    return "\n".join(out)

threading.Thread(target=sampler, daemon=True).start()

class H(BaseHTTPRequestHandler):
    def do_GET(self):
        try: body = translate(fetch()).encode(); code = 200
        except Exception as e: body = ("# adapter upstream error: %s\n" % e).encode(); code = 502
        self.send_response(code); self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body))); self.end_headers()
        self.wfile.write(body)
    def log_message(self, *a): pass
HTTPServer(("127.0.0.1", 8891), H).serve_forever()
