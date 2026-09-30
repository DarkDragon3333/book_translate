#!/usr/bin/env python3
"""Прокси-адаптер между bbm и YanoljaNEXT-Rosetta (родной формат модели вместо формата bbm).

bbm шлёт: system="" и user = [<glossary>…</glossary> + правила про метки +] «Please help me to translate,`текст`…»,
ждёт JSON {"ru_translation": "…"}. Rosetta обучена на другом формате:
  system: «Translate the user's text to Russian.» + Context/Tone + «Glossary:» «- term -> перевод» + «Output format: JSON»
  user:   JSON с исходным текстом, ответ — такой же JSON с переводом.
Прокси:
- отвечает на пробы bbm сам (PONG, schema_ok);
- вынимает из запроса bbm текст абзаца и термины глоссария, собирает запрос в формате Rosetta;
- из ответа берёт перевод (JSON {"text": …}; если JSON битый — текст как есть);
- чинит метки: имя восстанавливается по номеру из исходника (⟦кода5⟧ → ⟦code5⟧, ⟦var7⟧ остаётся ⟦var7⟧);
- если метки не сошлись или ответ пустой — один повтор с temperature 0;
- отдаёт bbm {"ru_translation": …}, пишет журнал (исходник, термины, сырой ответ, итог, метки, повтор).

Запуск внутри контейнера translate:
  python rosetta_proxy.py [--listen 127.0.0.1:8083] [--target http://ollama:11434] [--log /books/eval/rosetta_log.jsonl]
                          [--context "A technical book about software development."]
"""
import argparse, http.server, json, re, threading, time, urllib.request

a = argparse.ArgumentParser()
a.add_argument('--listen', default='127.0.0.1:8083')
a.add_argument('--target', default='http://ollama:11434')
a.add_argument('--log', default='/books/eval/rosetta_log.jsonl')
a.add_argument('--context', default='A technical book about software development.')
a.add_argument('--tone', default='Clear, natural technical prose, as in a professionally edited book.')
args = a.parse_args()
lock = threading.Lock()
opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

ASK = re.compile(r'Please help me to translate,`(.*)` to Russian, please return only translated content', re.S)
GLOSS = re.compile(r'<glossary>\n(.*?)\n</glossary>', re.S)
MARK = re.compile(r'⟦([a-z]+)(\d+)⟧')
# метка, которую модель испортила: ⟦ кода 5 ⟧, ⟦code5' (вместо ⟧ — апостроф из &#x27;)
ANY_MARK = re.compile(r'⟦\s*([^\W\d_]*)\s*(\d+)\s*(?:⟧|[\'’](?!⟧))')


def marks(t):
    return sorted(m.group(0) for m in MARK.finditer(t))


def fix_marks(src, out):
    names = {m.group(2): m.group(1) for m in MARK.finditer(src)}
    return ANY_MARK.sub(lambda m: '⟦%s%s⟧' % (names.get(m.group(2), m.group(1) or 'code'), m.group(2)), out)


def case_ok(term, src):
    """Термин без заглавных букв совпадает в любом регистре, с заглавными — только точно (целым словом)."""
    if term == term.lower():
        return True
    return re.search(r'(?<!\w)' + re.escape(term) + r'(?!\w)', src) is not None


def system_prompt(terms, has_marks):
    s = ["Translate the user's text to Russian.", 'Context: ' + args.context, 'Tone: ' + args.tone]
    if terms:
        s.append('Glossary:')
        s += ['- %s -> %s' % t for t in terms]
    if has_marks:
        s.append('Keep placeholders like ⟦code1⟧ and ⟦var2⟧ exactly as they are, at the right place.')
    s += ['Output format: JSON', 'Provide the final translation immediately without any other text.']
    return '\n'.join(s)


def parse(raw):
    raw = raw.strip()
    m = re.search(r'\{.*\}', raw, re.S)
    if m:
        try:
            v = json.loads(m.group(0)).get('text')
            if isinstance(v, str):
                return v.strip(), True
        except ValueError:
            pass
        # битый JSON (например, \x27 — такого экранирования в JSON нет): значение "text" вынимается вручную,
        # иначе в книгу попадал весь ответ с хвостом «"}»
        t = re.search(r'"text"\s*:\s*"(.*)"\s*\}\s*$', m.group(0), re.S)
        if t:
            v = re.sub(r'\\x27;?', "'", t.group(1))
            try:
                v = json.loads('"%s"' % v)
            except ValueError:
                v = v.replace('\\n', '\n').replace('\\"', '"').replace('\\\\', '\\')
            return v.strip(), False
    return raw, False


def call(model, system, text, temperature):
    body = json.dumps({'model': model, 'stream': False, 'options': {'temperature': temperature},
                       'messages': [{'role': 'system', 'content': system},
                                    {'role': 'user', 'content': json.dumps({'text': text}, ensure_ascii=False)}]}).encode()
    req = urllib.request.Request(args.target + '/api/chat', data=body, method='POST',
                                 headers={'Content-Type': 'application/json'})
    with opener.open(req, timeout=900) as r:
        return json.loads(r.read())['message']['content']


def reply(content, model):
    return {'id': 'chatcmpl-rosetta', 'object': 'chat.completion', 'created': int(time.time()), 'model': model,
            'choices': [{'index': 0, 'message': {'role': 'assistant', 'content': content}, 'finish_reason': 'stop'}],
            'usage': {'prompt_tokens': 0, 'completion_tokens': 0, 'total_tokens': 0}}


class H(http.server.BaseHTTPRequestHandler):
    def _send(self, code, data, ctype='application/json'):
        self.send_response(code)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _forward(self, body=None):
        req = urllib.request.Request(args.target + self.path, data=body, method=self.command,
                                     headers={k: v for k, v in self.headers.items()
                                              if k.lower() not in ('host', 'content-length', 'accept-encoding')})
        try:
            with opener.open(req, timeout=900) as r:
                self._send(r.status, r.read(), r.headers.get('Content-Type', 'application/json'))
        except urllib.error.HTTPError as e:
            self._send(e.code, e.read())

    def do_GET(self):
        self._forward()

    def do_POST(self):
        body = self.rfile.read(int(self.headers.get('Content-Length', 0)))
        if not self.path.startswith('/v1/chat/completions'):
            return self._forward(body)
        q = json.loads(body)
        model = q.get('model', '')
        user = [m['content'] for m in q.get('messages', []) if m.get('role') == 'user']
        last = user[-1] if user else ''
        fmt = json.dumps(q.get('response_format') or {})
        if last.startswith('Reply with the single word: PONG'):
            return self._send(200, json.dumps(reply('PONG.', model)).encode())
        if 'structured_output_probe' in fmt:
            return self._send(200, json.dumps(reply('{"probe": "schema_ok"}', model)).encode())
        m = ASK.search(last)
        if not m:
            return self._forward(body)
        src = m.group(1)
        g = GLOSS.search(last)
        terms = [tuple(x.split(' → ', 1)) for x in g.group(1).split('\n') if ' → ' in x] if g else []
        # bbm ищет термины без учёта регистра: «Windows» сработал бы на «context windows», «Spark» — на «spark».
        # Термин с заглавными буквами (название) передаём модели, только если он есть в тексте в точности так же.
        dropped = [t for t in terms if not case_ok(t[0], src)]
        terms = [t for t in terms if case_ok(t[0], src)]
        system = system_prompt(terms, bool(MARK.search(src)))
        tries = []
        for temp in (q.get('temperature', 0.2), 0.0):
            try:
                raw = call(model, system, src, temp)
            except Exception as e:
                return self._send(502, json.dumps({'error': str(e)}).encode())
            out, js = parse(raw)
            out = fix_marks(src, out)
            ok = marks(src) == marks(out) and bool(out)
            tries.append({'raw': raw, 'out': out, 'json': js, 'marks_ok': ok})
            if ok:
                break
        best = next((t for t in tries if t['marks_ok']), tries[-1])
        content = json.dumps({'ru_translation': best['out']}, ensure_ascii=False) if 'ru_translation' in fmt else best['out']
        self._send(200, json.dumps(reply(content, model), ensure_ascii=False).encode())
        with lock, open(args.log, 'a', encoding='utf-8') as f:
            f.write(json.dumps({'src': src, 'terms': terms, 'dropped': dropped, 'out': best['out'], 'marks_ok': best['marks_ok'],
                                'json': best['json'], 'retries': len(tries) - 1,
                                'raw': [t['raw'] for t in tries]}, ensure_ascii=False) + '\n')

    def log_message(self, *a):
        pass


host, port = args.listen.split(':')
print('rosetta_proxy: %s → %s, журнал %s' % (args.listen, args.target, args.log), flush=True)
http.server.ThreadingHTTPServer((host, int(port)), H).serve_forever()
