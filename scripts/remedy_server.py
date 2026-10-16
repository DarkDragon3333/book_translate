#!/usr/bin/env python3
"""Сервис оценки перевода ReMedy-9B для гибридного режима (контейнер remedy, образ llama.cpp full-cuda).

Видеокарта одна (4 ГБ), поэтому llama-server с моделью запускается только на время оценки книги
и гасится по запросу /unload или после REMEDY_IDLE секунд простоя. В остальное время сервис почти не тратит
памяти, а видеопамять свободна для Ollama.

Оценка ReMedy = скрытое состояние последнего токена (после финальной нормы) · голова score.npy.
У llama.cpp нет головы-оценщика Gemma2, поэтому модель подготовлена из двух частей
(experiments/qe/remedy_local/prepare.py): тело — GGUF Gemma2ForCausalLM, голова — score.npy [1×3584].
Запрос — как в ReMedy (score.py v0.1.3) и как при проверке 13.10 (Spearman с bf16 0,997):
chat-шаблон Gemma, user: «Translate the following English text into natural, fluent Russian sentence…»,
assistant: перевод.

HTTP (порт REMEDY_PORT, только внутри сети compose):
  GET  /status   {"available", "loaded", "model", "error"}
  POST /score    {"pairs": [{"src": …, "mt": …}, …]} → {"scores": [число | null], "tokens": [n]}
                 null — пара длиннее окна модели (2048 токенов)
  POST /tokens   {"src": …, "mt": …} → {"ids": […]} (проверка токенизации)
  В /score и /tokens можно передать "extra_bos" (проверка, scripts/tools/remedy_check.py).
  POST /unload   выгрузить модель
Только стандартная библиотека Python.
"""
import json, os, struct, subprocess, sys, threading, time, urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

DIR = os.environ.get('REMEDY_DIR', '/models/remedy9b')
GGUF = os.path.join(DIR, os.environ.get('REMEDY_GGUF', 'remedy9b-Q5_K_M.gguf'))
HEAD = os.path.join(DIR, 'score.npy')
NGL = os.environ.get('REMEDY_NGL', '17')            # слоёв на GPU из 41: Q5_K_M — 17 (~3,9 ГБ VRAM)
THREADS = os.environ.get('REMEDY_THREADS', '6')
CTX = int(os.environ.get('REMEDY_CTX', '2048'))
IDLE = int(os.environ.get('REMEDY_IDLE', '120'))
PORT = int(os.environ.get('REMEDY_PORT', '8091'))
LLAMA = os.environ.get('LLAMA_SERVER', '/app/llama-server')
INNER = 8092                                         # llama-server внутри контейнера
# Сколько <bos> перед шаблоном (в шаблоне Gemma свой <bos>): HF-токенизатор при проверке 13.10 добавлял ещё один.
EXTRA_BOS = int(os.environ.get('REMEDY_EXTRA_BOS', '1'))
BOS_ID = 2

PROMPT = ("Translate the following {s} text into natural, fluent {t} sentence while preserving the original meaning:\n"
          "{s}:{src}\n{t}:")

opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
lock = threading.Lock()
state = {'proc': None, 'last': 0.0, 'error': None}
W = []


def log(*a):
    print('[remedy]', *a, flush=True)


def load_head():
    """score.npy без numpy: заголовок формата .npy и float32/float64 little-endian."""
    with open(HEAD, 'rb') as f:
        if f.read(6) != b'\x93NUMPY':
            raise ValueError('score.npy: не формат .npy')
        major = f.read(2)[0]
        hlen = struct.unpack('<H' if major == 1 else '<I', f.read(2 if major == 1 else 4))[0]
        hdr = f.read(hlen).decode('latin1')
        data = f.read()
    code = 'f' if "'<f4'" in hdr else 'd' if "'<f8'" in hdr else None
    if code is None or 'True' in hdr.split('fortran_order')[1][:10]:
        raise ValueError('score.npy: нужен <f4 или <f8, C-порядок (%s)' % hdr)
    return list(struct.unpack('<%d%s' % (len(data) // (4 if code == 'f' else 8), code), data))


def available():
    return os.path.isfile(GGUF) and os.path.isfile(HEAD)


def http(path, data=None, timeout=600):
    req = urllib.request.Request('http://127.0.0.1:%d%s' % (INNER, path),
                                 data=json.dumps(data).encode() if data is not None else None,
                                 headers={'Content-Type': 'application/json'} if data is not None else {})
    with opener.open(req, timeout=timeout) as r:
        return json.loads(r.read())


def start():
    """Запустить llama-server (под lock). Ждём готовности до 5 минут: модель 6 ГБ читается с диска."""
    p = state['proc']
    if p is not None and p.poll() is None:
        return
    if not available():
        raise RuntimeError('нет файлов модели ReMedy: %s и %s' % (GGUF, HEAD))
    if not W:
        W.extend(load_head())
    cmd = [LLAMA, '-m', GGUF, '--embeddings', '--pooling', 'last', '-ngl', NGL, '-c', str(CTX), '-b', str(CTX),
           '-ub', str(CTX), '-np', '1', '-t', THREADS, '--host', '127.0.0.1', '--port', str(INNER)]
    log('запуск llama-server:', ' '.join(cmd[1:]))
    state['proc'] = subprocess.Popen(cmd, stdout=sys.stdout, stderr=subprocess.STDOUT)
    t0 = time.time()
    while time.time() - t0 < 300:
        if state['proc'].poll() is not None:
            raise RuntimeError('llama-server завершился при запуске (код %s) — см. docker compose logs remedy'
                               % state['proc'].returncode)
        try:
            with opener.open('http://127.0.0.1:%d/health' % INNER, timeout=2) as r:
                if r.status == 200:
                    log('модель загружена за %.0f с' % (time.time() - t0))
                    return
        except Exception:
            pass
        time.sleep(1)
    stop()
    raise RuntimeError('llama-server не ответил за 5 минут')


def stop():
    p = state['proc']
    if p is not None and p.poll() is None:
        p.terminate()
        try:
            p.wait(30)
        except subprocess.TimeoutExpired:
            p.kill()
        log('модель выгружена')
    state['proc'] = None


def text(src, mt):
    # chat-шаблон Gemma 2: '<bos>' + '<start_of_turn>' + роль + '\n' + содержимое | trim + '<end_of_turn>\n'
    u = PROMPT.format(s='English', t='Russian', src=src).strip()
    return '<bos><start_of_turn>user\n%s<end_of_turn>\n<start_of_turn>model\n%s<end_of_turn>\n' % (u, mt.strip())


def tokens(src, mt, extra_bos=None):
    ids = http('/tokenize', {'content': text(src, mt), 'add_special': False, 'parse_special': True})['tokens']
    ids = [t['id'] if isinstance(t, dict) else t for t in ids]
    return [BOS_ID] * (EXTRA_BOS if extra_bos is None else extra_bos) + ids


def flat(v):
    while v and isinstance(v[0], list):
        v = v[-1]                                    # pooling last: один вектор, иногда в обёртке [[…]]
    return v


def score(src, mt, extra_bos=None):
    ids = tokens(src, mt, extra_bos)
    if len(ids) > CTX:
        return None, len(ids)
    res = http('/embeddings', {'content': ids, 'embd_normalize': -1})
    e = res[0]['embedding'] if isinstance(res, list) else res['data'][0]['embedding']
    e = flat(e)
    if len(e) != len(W):
        e = e[-len(W):]
    return sum(a * b for a, b in zip(e, W)), len(ids)


def idle_watch():
    while True:
        time.sleep(10)
        with lock:
            p = state['proc']
            if p is not None and p.poll() is None and time.time() - state['last'] > IDLE:
                log('простой %d с — выгружаю' % IDLE)
                stop()


class H(BaseHTTPRequestHandler):
    def send(self, code, obj):
        b = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        if self.path == '/status':
            p = state['proc']
            return self.send(200, {'available': available(), 'loaded': p is not None and p.poll() is None,
                                   'model': GGUF, 'error': state['error']})
        self.send(404, {'error': 'нет такого адреса'})

    def do_POST(self):
        try:
            body = json.loads(self.rfile.read(int(self.headers.get('Content-Length', 0))) or b'{}')
            if self.path == '/unload':
                with lock:
                    stop()
                return self.send(200, {'ok': True})
            if self.path in ('/score', '/tokens'):
                with lock:
                    start()
                    state['last'] = time.time()
                    if self.path == '/tokens':
                        return self.send(200, {'ids': tokens(body['src'], body['mt'], body.get('extra_bos'))})
                    sc, nt = [], []
                    for p in body.get('pairs', []):
                        s, n = score(p['src'], p['mt'], body.get('extra_bos'))
                        sc.append(None if s is None else round(s, 4))
                        nt.append(n)
                    state['last'] = time.time()
                    state['error'] = None
                return self.send(200, {'scores': sc, 'tokens': nt})
            return self.send(404, {'error': 'нет такого адреса'})
        except Exception as e:
            state['error'] = '%s: %s' % (type(e).__name__, e)
            log('ОШИБКА', state['error'])
            return self.send(500, {'error': state['error']})

    def log_message(self, *a):
        pass


if __name__ == '__main__':
    log('сервис оценки ReMedy: порт %d, модель %s (%s)' % (PORT, GGUF, 'есть' if available() else 'НЕТ — гибрид недоступен'))
    threading.Thread(target=idle_watch, daemon=True).start()
    ThreadingHTTPServer(('0.0.0.0', PORT), H).serve_forever()
