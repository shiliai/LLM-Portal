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
import json, os, re, threading, time, urllib.request
from collections import defaultdict
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
    # TensorFold 0.3 exposes ``stalled`` rather than a separate queue gauge.
    # Keep it as the best available waiting signal; newer engine builds can
    # add one of the aliases below without changing the dashboard contract.
    "tensorfold_requests_stalled": "vllm:num_requests_waiting",
    "tensorfold_decode_rounds_total": "vllm:spec_decode_num_decode_steps_total",
}
RUNNING_NAMES = (
    "tensorfold_requests_inflight",
    "tensorfold_requests_running",
    "tensorfold_requests_processing",
)
WAITING_NAMES = (
    "tensorfold_requests_waiting",
    "tensorfold_requests_queued",
    "tensorfold_requests_pending",
    "tensorfold_requests_deferred",
    "tensorfold_requests_stalled",
)
SAMPLE_RE = re.compile(
    r"^([a-zA-Z_:][a-zA-Z0-9_:]*)(\{[^}]*\})?\s+"
    r"([-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?)\s*(?:#.*)?$"
)
state = {"off": 0, "drafted": 0.0, "accepted": 0.0, "kv": None, "ema_tps": 0.0,
         "samples": [], "lock": threading.Lock()}
_gauges = {"out_tps": 0.0, "in_tps": 0.0, "running_max": 0, "waiting_max": 0,
           "has_running": False, "has_waiting": False}


def parse_samples(body):
    """Return Prometheus samples as ``(name, labels, value)`` tuples."""
    out = []
    for line in body.splitlines():
        if not line or line.startswith("#"):
            continue
        match = SAMPLE_RE.match(line)
        if not match:
            continue
        try:
            out.append((match.group(1), match.group(2) or "", float(match.group(3))))
        except ValueError:
            continue
    return out


def aggregate_engine_samples(samples):
    """Sum samples by metric name so multi-model labels remain visible."""
    values = defaultdict(float)
    for name, _labels, value in samples:
        if name.startswith("tensorfold_"):
            values[name] += value
    return dict(values)


def first_engine_value(values, names):
    """Use the first available activity alias, avoiding duplicate gauges."""
    for name in names:
        if name in values:
            return values[name], True
    return 0.0, False

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
    vals = aggregate_engine_samples(parse_samples(body))
    running, has_running = first_engine_value(vals, RUNNING_NAMES)
    waiting, has_waiting = first_engine_value(vals, WAITING_NAMES)
    now = time.time()
    with state["lock"]:
        s = state["samples"]
        s.append((now, vals.get("tensorfold_completion_tokens_total", 0.0),
                  vals.get("tensorfold_prompt_tokens_total", 0.0),
                  vals.get("tensorfold_cached_tokens_total", 0.0), running, waiting,
                  has_running, has_waiting))
        while s and now - s[0][0] > WINDOW + 5: s.pop(0)
        if len(s) >= 2:
            t0, g0, p0, c0, _, _, _, _ = s[0]
            t1, g1, p1, c1, _, _, _, _ = s[-1]
            dt = max(t1 - t0, 1e-6)
            _gauges["out_tps"] = max(0.0, (g1 - g0) / dt)
            if _gauges["running_max"] and state["ema_tps"]:
                _gauges["out_tps"] = _gauges["running_max"] * state["ema_tps"]
            _gauges["in_tps"] = max(0.0, ((p1 - c1) - (p0 - c0)) / dt)
        _gauges["running_max"] = max((v[4] for v in s if v[6]), default=0)
        _gauges["waiting_max"] = max((v[5] for v in s if v[7]), default=0)
        _gauges["has_running"] = any(v[6] for v in s)
        _gauges["has_waiting"] = any(v[7] for v in s)
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
    for name, labels, value in parse_samples(body):
        # Activity gauges are emitted from the sampler window below.  Do not
        # also emit per-model copies, otherwise VM instant queries average the
        # same request count twice when a node exposes multiple model labels.
        if name in RUNNING_NAMES or name in WAITING_NAMES:
            continue
        dst = MAP.get(name)
        if dst:
            out.append(f"{dst}{labels} {value:g}")
        if name == "tensorfold_prompt_tokens_total":
            out.append(f"vllm:prefix_cache_queries_total{labels} {value:g}")
        if name == "tensorfold_cached_tokens_total":
            out.append(f"vllm:cache_cached_prompt_tokens_total{labels} {value:g}")
    with state["lock"]:
        out.append("llamacpp:predicted_tokens_seconds %.2f" % _gauges["out_tps"])
        out.append("llamacpp:prompt_tokens_seconds %.2f" % _gauges["in_tps"])
        if _gauges["has_running"]:
            out.append("vllm:num_requests_running %.0f" % _gauges["running_max"])
        if _gauges["has_waiting"]:
            out.append("vllm:num_requests_waiting %.0f" % _gauges["waiting_max"])
        d, a = int(state["drafted"]), int(state["accepted"])
    out.append("vllm:spec_decode_num_draft_tokens_total %d" % d)
    out.append("vllm:spec_decode_num_accepted_tokens_total %d" % a)
    if state["kv"]:
        pages, free = state["kv"]
        out.append("vllm:kv_cache_usage_perc %.6f" % max(0.0, 1 - free / POOL_PAGES))
    out.append(body)
    return "\n".join(out)

class H(BaseHTTPRequestHandler):
    def do_GET(self):
        try: body = translate(fetch()).encode(); code = 200
        except Exception as e: body = ("# adapter upstream error: %s\n" % e).encode(); code = 502
        self.send_response(code); self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body))); self.end_headers()
        self.wfile.write(body)
    def log_message(self, *a): pass


def main():
    threading.Thread(target=sampler, daemon=True).start()
    HTTPServer(("127.0.0.1", 8891), H).serve_forever()


if __name__ == "__main__":
    main()
