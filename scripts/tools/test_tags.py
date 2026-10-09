#!/usr/bin/env python3
"""Проверка: обрывает ли rosetta-ru перевод на тексте вида </s> и помогает ли замена угловых скобок.
Ничего не меняет в книгах: берёт из журналов адаптера абзацы с <s>, </s>, <i>… и переводит каждый
тремя способами — как есть (A), со скобками ‹ › (B), с &lt; &gt; (C) — по 2 раза.
Текст книги не печатает: только длину ответа к исходнику, закрыт ли JSON, вернулись ли все теги, причину остановки.
Запуск (Ollama должна работать):
  docker.exe compose run --rm --no-deps --entrypoint python translate /scripts/tools/test_tags.py
"""
import glob, json, re, sys, urllib.request

OLLAMA = 'http://ollama:11434'
MODEL = 'rosetta-ru'
TAG = re.compile(r'</?[A-Za-z_][\w:-]*>')
opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def system_prompt(terms, has_marks):
    s = ["Translate the user's text to Russian.", 'Context: A technical book about software development.',
         'Tone: Clear, natural technical prose, as in a professionally edited book.']
    if terms:
        s.append('Glossary:')
        s += ['- %s -> %s' % tuple(t) for t in terms]
    if has_marks:
        s.append('Keep placeholders like ⟦code1⟧ and ⟦var2⟧ exactly as they are, at the right place.')
    s += ['Output format: JSON', 'Provide the final translation immediately without any other text.']
    return '\n'.join(s)


def call(system, text):
    body = json.dumps({'model': MODEL, 'stream': False, 'options': {'temperature': 0.2},
                       'messages': [{'role': 'system', 'content': system},
                                    {'role': 'user', 'content': json.dumps({'text': text}, ensure_ascii=False)}]}).encode()
    req = urllib.request.Request(OLLAMA + '/api/chat', data=body, headers={'Content-Type': 'application/json'})
    with opener.open(req, timeout=900) as r:
        d = json.loads(r.read())
    return d['message']['content'], d.get('done_reason', '?')


VARIANTS = {
    'A как есть': (lambda s: s, lambda s: s),
    'B ‹ ›': (lambda s: TAG.sub(lambda m: '‹' + m.group(0)[1:-1] + '›', s),
              lambda s: re.sub(r'‹(/?[A-Za-z_][\w:-]*)›', r'<\1>', s)),
    'C &lt;': (lambda s: TAG.sub(lambda m: '&lt;' + m.group(0)[1:-1] + '&gt;', s),
               lambda s: re.sub(r'&lt;(/?[A-Za-z_][\w:-]*)&gt;', r'<\1>', s)),
}

logs = sys.argv[1:] or glob.glob('/books/*/.rosetta_log.jsonl') + glob.glob('/books/.gui/**/.rosetta_log.jsonl', recursive=True)
n = 0
for path in logs:
    for line in open(path, encoding='utf-8'):
        if not line.strip():
            continue
        e = json.loads(line)
        tags = TAG.findall(e['src'])
        if not tags:
            continue
        n += 1
        print('\n%s, абзац %d символов, теги: %s' % (path, len(e['src']), ' '.join(sorted(set(tags)))))
        system = system_prompt(e.get('terms') or [], '⟦' in e['src'])
        for name, (enc, dec) in VARIANTS.items():
            for k in (1, 2):
                raw, why = call(system, enc(e['src']))
                closed = bool(re.search(r'\}\s*$', raw.strip()))
                try:
                    out = json.loads(raw.strip()).get('text', '')
                except ValueError:
                    out = raw
                out = dec(out)
                back = all(out.count(t) >= e['src'].count(t) for t in set(tags))
                print('  %-10s прогон %d: длина %.2f, JSON закрыт %s, теги вернулись %s, остановка %s'
                      % (name, k, len(out) / len(e['src']), 'да' if closed else 'НЕТ', 'да' if back else 'НЕТ', why))
print('\nабзацев с тегами: %d' % n)
