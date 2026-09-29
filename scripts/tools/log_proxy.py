#!/usr/bin/env python3
"""Прокси-«магнитофон» между bbm и Ollama: пересылает запросы как есть и пишет их в файл.

Нужен, чтобы увидеть ровно то, что bbm отправляет модели (системное сообщение,
приписки, термины, метки кода) — в этом же формате потом строятся обучающие примеры.
Запускается внутри контейнера конвейера, а bbm направляется на него вместо Ollama (--api_base http://127.0.0.1:8081/v1):
  python /scripts/tools/log_proxy.py [--listen 127.0.0.1:8081] [--target http://ollama:11434] [--log /books/eval/bbm_requests.jsonl]
"""
import argparse, http.server, json, threading, urllib.request

a = argparse.ArgumentParser()
a.add_argument('--listen', default='127.0.0.1:8081')
a.add_argument('--target', default='http://ollama:11434')
a.add_argument('--log', default='/books/eval/bbm_requests.jsonl')
args = a.parse_args()
lock = threading.Lock()
opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # без системных прокси


class H(http.server.BaseHTTPRequestHandler):
    def _forward(self, body=None):
        req = urllib.request.Request(args.target + self.path, data=body, method=self.command,
                                     headers={k: v for k, v in self.headers.items()
                                              if k.lower() not in ('host', 'content-length', 'accept-encoding')})
        try:
            with opener.open(req, timeout=900) as r:
                data, code, ctype = r.read(), r.status, r.headers.get('Content-Type', 'application/json')
        except urllib.error.HTTPError as e:
            data, code, ctype = e.read(), e.code, 'application/json'
        self.send_response(code)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)
        return data

    def do_GET(self):
        self._forward()

    def do_POST(self):
        body = self.rfile.read(int(self.headers.get('Content-Length', 0)))
        try:
            resp = self._forward(body)
        except Exception as e:  # запрос всё равно записываем
            resp = ('{"error": "%s"}' % e).encode()
            self.send_error(502)
        try:
            rec = {'path': self.path, 'request': json.loads(body)}
            try:
                rec['response'] = json.loads(resp)
            except ValueError:
                rec['response_raw'] = resp.decode('utf-8', 'replace')[:20000]
            with lock, open(args.log, 'a', encoding='utf-8') as f:
                f.write(json.dumps(rec, ensure_ascii=False) + '\n')
        except ValueError:
            pass

    def log_message(self, *a):
        pass


host, port = args.listen.split(':')
print('log_proxy: %s → %s, лог %s' % (args.listen, args.target, args.log), flush=True)
http.server.ThreadingHTTPServer((host, int(port)), H).serve_forever()
