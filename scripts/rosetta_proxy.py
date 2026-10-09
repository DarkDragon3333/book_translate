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
- если метки не сошлись, ответ пустой или не JSON — повтор с temperature 0 (метки перечисляются явно),
  если метки всё ещё не сошлись — ещё один повтор (иначе bbm дописывает потерянный код в конец абзаца);
- слова-код из заголовков (карта --keep) передаёт модели глоссарием «switch -> switch»;
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
a.add_argument('--senses', help='карта смыслов книги (.senses.json от scripts/senses.py)')
a.add_argument('--senses-rules', default='/config/senses.csv')
a.add_argument('--keep', help='карта кода в заголовках {ключ заголовка: [слова]} от prep_epub.py --keep-map')
args = a.parse_args()
lock = threading.Lock()
opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

ASK = re.compile(r'Please help me to translate,`(.*)` to Russian, please return only translated content', re.S)
GLOSS = re.compile(r'<glossary>\n(.*?)\n</glossary>', re.S)
MARK = re.compile(r'⟦([a-z]+)(\d+)⟧')
# метка, которую модель испортила. Видели: ⟦ кода 5 ⟧, ⟦code5' (апостроф из &#x27;), ⟦code48], ⟦code136;], ⟦code227;⟧,
# ⟦var135» (модель закрыла метку кавычкой «…»), ⟦в степени 83⟧ (имя метки переведено).
# Имя — до 3 слов, номер обязателен; закрывающая: ⟧ | ] | » (кавычка возвращается в текст) | апостроф.
ANY_MARK = re.compile(r'⟦\s*((?:[^\W\d_]+\s+){0,2}[^\W\d_]*)\s*(\d+)\s*;?\s*(⟧|\]|»|[\'’](?!⟧))')


# Текст вида <s>, </s>, <i> в абзаце (не код, а обозначения в прозе): на «</s>» Rosetta (Gemma 3) обрывает ответ.
# Модели такие теги уходят как &lt;s&gt; и возвращаются обратно. Тест 08.10 (test_tags.py, 2 абзаца Géron × 2 прогона):
# как есть — обрыв 4/4; ‹s› — теги потеряны 2/4; &lt;s&gt; — целиком и с тегами 4/4.
TAG = re.compile(r'</?[A-Za-z_][\w:-]*>')


def esc_tags(src):
    if '&lt;' in src or '&gt;' in src:           # в тексте уже есть сущности — не трогаем, обратно не отличить
        return src
    return TAG.sub(lambda m: '&lt;' + m.group(0)[1:-1] + '&gt;', src)


def unesc_tags(src, out):
    names = {t[1:-1] for t in TAG.findall(src)}
    if not names or '&lt;' in src or '&gt;' in src:
        return out
    return re.sub(r'&lt;(/?[A-Za-z_][\w:-]*)&gt;', lambda m: '<%s>' % m.group(1) if m.group(1) in names else m.group(0), out)


KEEP = {}
if args.keep:
    try:
        KEEP = json.load(open(args.keep, encoding='utf-8'))
    except Exception as e:
        print('rosetta_proxy: карта кода в заголовках не загружена: %s' % e, flush=True)


def head_key(text):
    """Тот же ключ, что в prep_epub.head_key: только буквы и цифры, нижний регистр, без меток."""
    return re.sub(r'[\W_]+', '', MARK.sub('', text)).lower()


def marks(t):
    return sorted(m.group(0) for m in MARK.finditer(t))


def fix_marks(src, out):
    names = {m.group(2): m.group(1) for m in MARK.finditer(src)}
    out = re.sub(r'(⟦[^⟧\s]{0,20}\d+)\\(?:x|u00|u)27;?', r"\1'", out)   # ⟦code783\u27; — апостроф, экранированный моделью

    def sub(m):
        name = names.get(m.group(2))
        if name is None and not re.fullmatch(r'[a-z]*', m.group(1).strip()):
            return m.group(0)                    # номера нет в исходнике и имя не латиница — не трогаем
        return '⟦%s%s⟧' % (name or m.group(1).strip() or 'code', m.group(2)) + ('»' if m.group(3) == '»' else '')
    return ANY_MARK.sub(sub, out)


# Смысл многозначных терминов (feature, transformer, recall…): карта строится до перевода по всей книге
# (scripts/senses.py), здесь по абзацу берётся решение: свой перевод / перевод другого смысла / не подсказывать.
SMAP, SRULES = {}, {}
if args.senses:
    try:
        import os, sys
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        import senses as _senses
        SMAP = json.load(open(args.senses, encoding='utf-8'))
        SRULES = {w: r['forms'] for w, r in _senses.load_senses(args.senses_rules).items()}
    except Exception as e:                       # без карты адаптер работает как раньше
        print('rosetta_proxy: карта смыслов не загружена: %s' % e, flush=True)
        SMAP, SRULES = {}, {}


def apply_senses(src, terms):
    """Возвращает (термины, изменения, слова без решения). Изменения: [слово, 'alt'|'none', перевод]."""
    if not SRULES:
        return terms, [], []
    entry = _senses.lookup(SMAP, _senses.key(src))
    changes, missing, out = [], [], []
    for t in terms:
        w = next((w for w, rx in SRULES.items() if rx.fullmatch(t[0])), None)
        if w is None:
            out.append(t)
            continue
        if entry is None or w not in entry:
            missing.append(w)
            out.append(t)
            continue
        kind, tgt = entry[w]
        if kind == 'own':
            out.append(t)
        elif kind == 'alt' and tgt:
            out.append((t[0], tgt))
            changes.append([w, 'alt', tgt])
        else:
            changes.append([w, 'none', None])
    return out, changes, missing


def case_ok(term, src):
    """Термин без заглавных букв совпадает в любом регистре, с заглавными — только точно (целым словом)
    или с заглавной первой буквой в начале предложения/заголовка («Inclusive OR» для «inclusive OR»)."""
    if term == term.lower():
        return True
    forms = {term, term[0].upper() + term[1:]}
    return any(re.search(r'(?<!\w)' + re.escape(t) + r'(?!\w)', src) for t in forms)


def has_word(text, w):
    return re.search(r'(?<![\w-])' + re.escape(w) + r'(?![\w(-])', text) is not None


def unquote_keep(out, keep):
    """`switch`, 'for' → switch, for: кавычки и обратные апострофы вокруг слов-кода из заголовка
    (курсив вернёт restore_epub)."""
    for w in keep:
        out = re.sub(r'([`\'"])(%s)\1' % re.escape(w), r'\2', out)
    return out


def system_prompt(terms, has_marks, need=None, lost=None):
    s = ["Translate the user's text to Russian.", 'Context: ' + args.context, 'Tone: ' + args.tone]
    if terms:
        s.append('Glossary:')
        s += ['- %s -> %s' % t for t in terms]
    if has_marks:
        s.append('Keep placeholders like ⟦code1⟧ and ⟦var2⟧ exactly as they are, at the right place.')
    if need:
        s.append('The translation must contain each of these placeholders exactly once: %s.' % ', '.join(need))
    if lost:
        s.append('Keep these words in English exactly as written (they are code): %s.' % ', '.join(lost))
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
            return tidy(v), False
    # ответ оборван (Rosetta останавливается на «</s>» в тексте): снять начало {"text": ", иначе оно уходит в книгу
    t = re.match(r'\{\s*"text"\s*:\s*"(.*)$', raw, re.S)
    if t:
        return tidy(t.group(1).replace('\\n', '\n').replace('\\"', '"')), False
    return raw, False


def tidy(v):
    """Хвосты битого JSON: «Построение циклов для"», «Коллекторы ти",», «:"Пропуск данных»."""
    v = re.sub(r'^\s*:\s*"', '', v.strip())
    v = re.sub(r'"\s*[,}]*\s*$', '', v)
    return v.strip()


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
        terms, senses_changed, senses_missing = apply_senses(src, terms)
        keep = [w for w in KEEP.get(head_key(src), []) if w not in {t[0] for t in terms}]
        terms = terms + [(w, w) for w in keep]
        has_marks = bool(MARK.search(src))
        send = esc_tags(src)
        tries = []
        for n, temp in enumerate((q.get('temperature', 0.2), 0.0, 0.4)):
            if n == 2 and tries[-1]['marks_ok']:
                break                              # третья попытка — только ради меток
            need = marks(src) if n and not tries[-1]['marks_ok'] else None
            lost = [w for w in keep if not has_word(tries[-1]['out'], w)] if n else []
            try:
                raw = call(model, system_prompt(terms, has_marks, need, lost), send, temp)
            except Exception as e:
                return self._send(502, json.dumps({'error': str(e)}).encode())
            out, js = parse(raw)
            out = unquote_keep(fix_marks(src, unesc_tags(src, out)), keep)
            ok = marks(src) == marks(out) and bool(out)
            kept = all(has_word(out, w) for w in keep)
            tries.append({'raw': raw, 'out': out, 'json': js, 'marks_ok': ok, 'keep_ok': kept})
            if ok and js and kept:
                break
        rank = lambda t: (t['marks_ok'], t['json'], t['keep_ok'])
        best = max(tries, key=rank)               # при равенстве — более ранняя попытка
        content = json.dumps({'ru_translation': best['out']}, ensure_ascii=False) if 'ru_translation' in fmt else best['out']
        self._send(200, json.dumps(reply(content, model), ensure_ascii=False).encode())
        with lock, open(args.log, 'a', encoding='utf-8') as f:
            f.write(json.dumps({'src': src, 'terms': terms, 'dropped': dropped, 'keep': keep, 'senses': senses_changed,
                                'senses_missing': senses_missing, 'out': best['out'], 'marks_ok': best['marks_ok'],
                                'json': best['json'], 'keep_ok': best['keep_ok'], 'retries': len(tries) - 1,
                                'raw': [t['raw'] for t in tries]}, ensure_ascii=False) + '\n')

    def log_message(self, *a):
        pass


host, port = args.listen.split(':')
print('rosetta_proxy: %s → %s, журнал %s' % (args.listen, args.target, args.log), flush=True)
http.server.ThreadingHTTPServer((host, int(port)), H).serve_forever()
