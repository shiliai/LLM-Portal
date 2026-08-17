"""mock LiteLLM:回放生产形状的 /key/list,支持 /key/update 改内存状态。
数据来源:环境变量 KEYLIST_JSON 指向导出文件;缺省用内置合成夹具(与生产同形状,
含密钥哈希/别名/分组/禁用态,不依赖任何生产导出数据)。
站点模型管理 e2e 另需:/model/info + /model/new + /model/delete(内存 deployment
状态)、/onboard/admin/list(兼扮 onboardd)、无 Authorization 的 /v1/models
(兼扮站点上游——与网关自身 /v1/models 按 Authorization 头区分,与单测同判据)。"""
import hashlib, json, os, secrets, sys, time
from http.server import BaseHTTPRequestHandler, HTTPServer

_fixture_path = os.environ.get("KEYLIST_JSON")
if _fixture_path and os.path.exists(_fixture_path):
    STATE = json.load(open(_fixture_path))
    if isinstance(STATE, dict):
        STATE = STATE.get('keys', STATE.get('data', []))
else:
    if _fixture_path:
        print(f'warning: KEYLIST_JSON={_fixture_path} 不存在,回退内置合成夹具', file=sys.stderr)
    _aliases = ['site-a-cli', 'home-only', 'e2e-user', 'ci-runner']
    _groups = ['default', 'home', 'default', 'lab']
    STATE = []
    for _alias, _group in zip(_aliases, _groups):
        _tok = hashlib.sha256(('sk-fixture-' + _alias).encode()).hexdigest()
        STATE.append({"token": _tok, "key_name": None, "key_alias": _alias,
                      "metadata": {"group": _group}, "models": [],
                      "blocked": None if _alias != 'home-only' else False,
                      "created_at": "2026-08-15T08:00:00"})

# 站点模型管理夹具:一个 active 站点 + 一个旧 id 的 qwen deployment,
# 上游 /v1/models 返回新 id(两个,逼出「多 id 下拉」分支)。wg_ip 用 127.0.0.1:
# console 的探测是服务端 httpx 真发起的,本机回环才可达(另起 MOCK_PORT=8004
# 的 mock 实例扮站点上游)
DEP_SEQ = [1]     # 初始夹具已占 dep-1,新建从 dep-2 起(撞 id 会把多个一起删)
DEPLOYMENTS = [
    {"model_name": "qwen3.6-35b-fp8",
     "litellm_params": {"model": "openai/qwen3.6-35b-fp8",
                        "api_base": "http://127.0.0.1:8004/v1", "tags": ["home"]},
     "model_info": {"id": "dep-1"}},
]
UPSTREAM_MODELS = {"data": [{"id": "qwen3.8-27b-mtp2", "owned_by": "llamacpp"},
                            {"id": "qwen3.8-27b-mtp2-mini", "owned_by": "llamacpp"}]}
ONBOARD_SITES = {"sites": [{"name": "workstation", "pubkey": "MOCKPUBKEY0=",
                            "wg_ip": "127.0.0.1", "models": "[]",
                            "groups": '["home"]', "status": "active",
                            "created_at": 1755400000}]}

class H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def _send(self, obj, code=200):
        b = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header('content-type', 'application/json')
        self.send_header('content-length', str(len(b)))
        self.end_headers()
        self.wfile.write(b)
    def do_GET(self):
        if self.path.startswith('/key/list'):
            self._send(STATE)
        elif self.path.startswith('/onboard/admin/list'):
            self._send(ONBOARD_SITES)
        elif self.path.startswith('/model/info'):
            self._send({"data": DEPLOYMENTS})
        elif self.path.startswith('/v1/models') and not self.headers.get('Authorization'):
            self._send(UPSTREAM_MODELS)            # 站点上游探测(无鉴权头)
        elif self.path.startswith('/v1/models'):
            self._send({"data": [{"id": d["model_name"]} for d in DEPLOYMENTS]})
        elif self.path.startswith('/key/info'):
            self._send({"info": {"key_alias": "x", "metadata": {"group": "default"}, "models": []}})
        elif self.path.startswith('/spend/logs'):
            import time as _t, datetime as _dt
            def mk(i):
                start = _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(seconds=i * 2700)
                st = start.strftime('%Y-%m-%dT%H:%M:%S.000000Z')
                ak = 'litellm_proxy_master_key' if i % 5 == 0 else STATE[i % len(STATE)]['token']
                fail = (i % 9 == 0)
                tft = 300 + (i * 97) % 2400
                md = ({'error_str': 'upstream connect timeout after 5000ms'}
                      if fail else {'usage_object': {'prompt_tokens_details': {'cached_tokens': (i * 37) % 900}}})
                eff = ['high', 'medium', 'budget:8192', ''][i % 4]  # 1/4 行未携带 effort
                if eff:
                    md['spend_logs_metadata'] = {'effort': eff}   # 生产落库形态（写库白名单内）
                return {'startTime': st,
                        'completionStartTime': (start + _dt.timedelta(milliseconds=tft)).strftime('%Y-%m-%dT%H:%M:%S.000000Z'),
                        'endTime': st, 'api_key': ak,
                        'requester_ip_address': ['198.51.100.7', '203.0.113.12', '192.0.2.20'][i % 3],
                        'model_group': ['deepseek-v4-flash-0731', 'qwen3.6-35b-fp8'][i % 2],
                        'call_type': 'messages' if i % 3 == 0 else 'completion',
                        'prompt_tokens': 1000 + i * 17, 'completion_tokens': 200 + i * 5,
                        'request_duration_ms': tft + 200 + (i * 173) % 4000,
                        'status': 'failure' if fail else 'success',
                        'request_id': 'req-%04d' % i, 'session_id': 'sess-%02d' % (i % 4),
                        'metadata': md}
            self._send({'data': [mk(i) for i in range(30)]})
        else:
            self._send({"data": []})
    def do_POST(self):
        n = int(self.headers.get('content-length') or 0)
        body = json.loads(self.rfile.read(n) or b'{}')
        if self.path.startswith('/model/new'):
            DEP_SEQ[0] += 1
            dep = {"model_name": body.get("model_name"),
                   "litellm_params": body.get("litellm_params") or {},
                   "model_info": {"id": "dep-%d" % DEP_SEQ[0]}}
            DEPLOYMENTS.append(dep)
            return self._send({"ok": True, "model_info": dep["model_info"]})
        if self.path.startswith('/model/delete'):
            DEPLOYMENTS[:] = [d for d in DEPLOYMENTS
                              if (d.get("model_info") or {}).get("id") != body.get("id")]
            return self._send({"ok": True})
        if self.path.startswith('/onboard/admin/models'):
            return self._send({"ok": True})
        if self.path.startswith('/key/generate'):
            import secrets as _s, hashlib as _h, time as _t
            key = 'sk-' + _s.token_urlsafe(24)
            h = _h.sha256(key.encode()).hexdigest()
            STATE.append({"token": h, "key_name": None,
                          "key_alias": body.get("key_alias"),
                          "metadata": body.get("metadata") or {},
                          "models": body.get("models") or [], "blocked": None,
                          "created_at": _t.strftime("%Y-%m-%dT%H:%M:%S")})
            return self._send({"key": key, "token": h})
        if self.path.startswith('/key/update'):
            for k in STATE:
                info = k.get('key_info', k)
                if info.get('token') == body.get('key'):
                    if 'metadata' in body: info['metadata'] = body['metadata']
                    if 'key_alias' in body: info['key_alias'] = body['key_alias']
                    if 'models' in body: info['models'] = body['models']
                    return self._send({"key": body.get('key'), "updated": True})
            return self._send({"error": "not found"}, 404)
        if self.path.startswith('/key/block') or self.path.startswith('/key/unblock'):
            blocked = self.path.startswith('/key/block')
            for k in STATE:
                info = k.get('key_info', k)
                if info.get('token') == body.get('key'):
                    info['blocked'] = blocked
                    return self._send({**info, 'blocked': blocked})
            return self._send({"error": "not found"}, 404)
        if self.path.startswith('/key/delete'):
            gone = set(body.get('keys') or [])
            STATE[:] = [k for k in STATE if (k.get('key_info', k)).get('token') not in gone]
            return self._send({"ok": True})
        self._send({"ok": True})

HTTPServer(('127.0.0.1', int(os.environ.get('MOCK_PORT', '4100'))), H).serve_forever()
