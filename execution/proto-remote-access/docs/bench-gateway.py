#!/usr/bin/env python3
"""private-llm 网关 vs 直连 基准（自包含，可反复跑）。
测：隧道丢包、直连冷/热、网关热（8K/32K token 量级）、短请求 keep-alive 开销。
判定口径：网关增量（gw_warm - direct_warm）是否 ≈ 1-2 RTT（跨境 RTT≈170ms）。"""
import http.client, json, ssl, time, urllib.request

KEY = "sk-REDACTED-ROTATED-2026-08-16"
GW = "https://llm-portal.example.com"
DIRECT = "http://192.0.2.10:8890"

def post(url, headers, body, timeout=600):
    req = urllib.request.Request(url, data=body, headers=headers)
    t0 = time.time()
    urllib.request.urlopen(req, timeout=timeout).read()
    return time.time() - t0

def bench(tag, reps):
    filler = "基准填充内容用于前缀缓存归因测试，" * reps
    body = json.dumps({"model": "deepseek-v4-flash-0731", "stream": False, "max_tokens": 5,
                       "messages": [{"role": "system", "content": f"标识{tag}-{int(time.time())}。\n{filler}"},
                                    {"role": "user", "content": "回复OK"}]}).encode()
    H = {"Authorization": "Bearer none", "content-type": "application/json"}
    G = {"Authorization": f"Bearer {KEY}", "content-type": "application/json"}
    dc = post(DIRECT + "/v1/chat/completions", H, body)
    dw = post(DIRECT + "/v1/chat/completions", H, body)
    ctx = ssl.create_default_context()
    conn = http.client.HTTPSConnection("llm-portal.example.com", context=ctx, timeout=600)
    gws = []
    for _ in range(3):
        t0 = time.time()
        conn.request("POST", "/v1/chat/completions", body=body, headers=G)
        r = conn.getresponse(); r.read()
        gws.append(time.time() - t0)
    conn.close()
    gw_min = min(gws)
    print(f"[{tag} {len(body)//1024}KB] direct冷={dc:.2f}s direct热={dw:.2f}s "
          f"gw热={gw_min:.2f}~{max(gws):.2f}s 增量={gw_min-dw:+.2f}s")

bench("8k", 800)
bench("32k", 3200)

body = json.dumps({"model": "deepseek-v4-flash-0731", "stream": False, "max_tokens": 5,
                   "messages": [{"role": "user", "content": "回复OK"}]}).encode()
ctx = ssl.create_default_context()
conn = http.client.HTTPSConnection("llm-portal.example.com", context=ctx, timeout=120)
ts = []
for _ in range(5):
    t0 = time.time()
    conn.request("POST", "/v1/chat/completions", body=body,
                 headers={"Authorization": f"Bearer {KEY}", "content-type": "application/json"})
    r = conn.getresponse(); r.read()
    ts.append(time.time() - t0)
conn.close()
print(f"[短请求63B keepalive×5] {min(ts[1:]):.2f}~{max(ts[1:]):.2f}s（首连 {ts[0]:.2f}s 含 TLS）")
print("判定：gw热增量 ≤0.7s（≈2 RTT+LiteLLM）且 32K 增量随丢包回落 → 达标；仍 >3s → 物理链路约束")
