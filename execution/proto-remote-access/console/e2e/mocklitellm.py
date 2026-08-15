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
            self._send({"data": []})
        else:
            self._send({"data": []})
    def do_POST(self):
        n = int(self.headers.get('content-length') or 0)
        body = json.loads(self.rfile.read(n) or b'{}')
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
