#!/usr/bin/env python3
"""Заголовки с контекстом: помогает ли Rosetta следующий абзац в поле Context (проверка 13.10).

Заголовки без контекста — самое ошибочное место 4B (на единицу текста ошибок в 7 раз больше: «Training Models» →
«Модели обучения», «Bias» → «Предвзятость»); 12B ошибается в тех же местах. Скрипт берёт заголовки из журналов
адаптера (короткая строка без точки в конце) и переводит каждый дважды тем же запросом, что rosetta_proxy:
  A — Context как в конвейере;  B — Context + «This is a heading; the text under it begins: <следующий абзац>».
Оба варианта с temperature 0 (без случайности), термины — те же, что были в журнале. Книгу не переводит.

  python heading_context_test.py журнал1.jsonl [журнал2.jsonl …] --out /books/.qe/headings.jsonl [--model rosetta-ru] [--max 600]
"""
import argparse, json, re, time, urllib.request

CONTEXT = 'A technical book about software development.'
TONE = 'Clear, natural technical prose, as in a professionally edited book.'
MARK = re.compile(r'⟦[^⟧]*⟧')
SKIP = {'note', 'tip', 'warning', 'caution', 'important', 'exercises', 'summary'}


def system(terms, extra=None, has_marks=False):
    s = ["Translate the user's text to Russian.", 'Context: ' + CONTEXT + (' ' + extra if extra else ''), 'Tone: ' + TONE]
    if terms:
        s.append('Glossary:')
        s += ['- %s -> %s' % tuple(t) for t in terms]
    if has_marks:
        s.append('Keep placeholders like ⟦code1⟧ and ⟦var2⟧ exactly as they are, at the right place.')
    s += ['Output format: JSON', 'Provide the final translation immediately without any other text.']
    return '\n'.join(s)


def call(url, model, sys_, text):
    q = {'model': model, 'stream': False, 'options': {'temperature': 0},
         'messages': [{'role': 'system', 'content': sys_},
                      {'role': 'user', 'content': json.dumps({'text': text}, ensure_ascii=False)}]}
    req = urllib.request.Request(url + '/api/chat', data=json.dumps(q).encode(), headers={'Content-Type': 'application/json'})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(req, timeout=300) as r:
        raw = json.loads(r.read())['message']['content'].strip()
    m = re.search(r'\{.*\}', raw, re.S)
    if m:
        try:
            return json.loads(m.group(0)).get('text', raw).strip()
        except ValueError:
            pass
    return raw


def is_heading(src):
    t = MARK.sub('', src).strip()
    return 2 <= len(t) < 80 and not re.search(r'[.:;?!,]\s*$', t) and t.lower() not in SKIP and re.search('[A-Za-z]{3}', t)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('logs', nargs='+')
    ap.add_argument('--out', required=True)
    ap.add_argument('--model', default='rosetta-ru')
    ap.add_argument('--url', default='http://ollama:11434')
    ap.add_argument('--max', type=int, default=600)
    a = ap.parse_args()
    items = []
    for f in a.logs:
        L = [json.loads(l) for l in open(f, encoding='utf-8') if l.strip()]
        for i, r in enumerate(L):
            if not is_heading(r['src']):
                continue
            nxt = next((x['src'] for x in L[i + 1:i + 4] if not is_heading(x['src'])), '')
            if len(MARK.sub('', nxt).strip()) < 40:
                continue
            items.append({'log': f, 'n': i, 'src': r['src'], 'terms': r.get('terms', []), 'old': r['out'],
                          'next': MARK.sub('', nxt)[:400].strip()})
    step = max(1, len(items) // a.max) if len(items) > a.max else 1
    items = items[::step][:a.max]
    print('Заголовков: %d (из журналов: %s)' % (len(items), ', '.join(a.logs)), flush=True)
    t0, changed = time.time(), 0
    with open(a.out, 'w', encoding='utf-8') as f:
        for k, it in enumerate(items):
            hm = bool(MARK.search(it['src']))
            it['a'] = call(a.url, a.model, system(it['terms'], None, hm), it['src'])
            it['b'] = call(a.url, a.model, system(it['terms'], 'This is a heading; the text under it begins: "%s"' % it['next'], hm), it['src'])
            changed += it['a'] != it['b']
            f.write(json.dumps(it, ensure_ascii=False) + '\n')
            if (k + 1) % 25 == 0:
                print('  %d/%d, %.1f с/заголовок, перевод изменился у %d' % (k + 1, len(items), (time.time() - t0) / (k + 1), changed), flush=True)
    print('Готово: %d заголовков, с контекстом перевод другой у %d → %s' % (len(items), changed, a.out))


if __name__ == '__main__':
    main()
