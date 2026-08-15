"""mock LiteLLM:回放生产形状的 /key/list,支持 /key/update 改内存状态"""
import json, sys
from http.server import BaseHTTPRequestHandler, HTTPServer

STATE = json.load(open('/tmp/e2e/keylist.json'))
if isinstance(STATE, dict):
    STATE = STATE.get('keys', STATE.get('data', []))

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
                return {'startTime': st,
                        'completionStartTime': (start + _dt.timedelta(milliseconds=tft)).strftime('%Y-%m-%dT%H:%M:%S.000000Z'),
                        'endTime': st, 'api_key': ak,
                        'requester_ip_address': ['112.10.88.23', '192.168.88.12', '172.18.0.2'][i % 3],
                        'model_group': ['deepseek-v4-flash-0731', 'qwen3.6-35b-fp8'][i % 2],
                        'call_type': 'messages' if i % 3 == 0 else 'completion',
                        'prompt_tokens': 1000 + i * 17, 'completion_tokens': 200 + i * 5,
                        'request_duration_ms': tft + 200 + (i * 173) % 4000,
                        'status': 'failure' if fail else 'success',
                        'request_id': 'req-%04d' % i, 'session_id': 'sess-%02d' % (i % 4),
                        'metadata': ({'error_str': 'upstream connect timeout after 5000ms'}
                                     if fail else {'usage_object': {'prompt_tokens_details': {'cached_tokens': (i * 37) % 900}}})}
            self._send({'data': [mk(i) for i in range(30)]})
        else:
            self._send({"data": []})
    def do_POST(self):
        n = int(self.headers.get('content-length') or 0)
        body = json.loads(self.rfile.read(n) or b'{}')
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
        self._send({"ok": True})

HTTPServer(('127.0.0.1', 4100), H).serve_forever()
