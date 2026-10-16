#!/usr/bin/env python3
"""Заголовки с темой книги в Context (тест 13.10, продолжение heading_context_test.py).

Сейчас адаптер пишет в Context для любой книги «A technical book about software development.» — и для Géron (ML).
Скрипт берёт заголовки из headings.jsonl прошлого теста (там уже есть вариант A — как в конвейере, temperature 0)
и переводит их ещё раз (вариант C): тот же запрос, но Context — по темам глоссариев книги, как rosetta_proxy --domains:
  ml,math → «A technical book about machine learning and mathematics.»
Следующий абзац модели не передаётся (в тесте B он протекал в заголовок). Книгу не переводит.

  python heading_topic_test.py /books/geron_ch10_g2/headings.jsonl --out /books/geron_ch10_g2/headings_c.jsonl
         [--domains ml,math] [--map Spring=programming,web] [--model rosetta-ru]
--map ЧАСТЬ_ПУТИ=темы — для заголовков из журнала, в пути которого есть ЧАСТЬ_ПУТИ (по умолчанию Spring → programming,web).
"""
import argparse, json, os, sys, time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from heading_context_test import call, system, MARK   # тот же запрос, что в прошлом тесте

TOPICS = {'programming': 'programming', 'web': 'web development', 'ml': 'machine learning', 'math': 'mathematics',
          'devops': 'DevOps and cloud infrastructure', 'distributed': 'distributed systems',
          'security': 'information security', 'gamedev': 'game development', 'lowlevel': 'low-level systems programming'}


def topic_context(domains):     # как в rosetta_proxy.py
    names = []
    for d in domains.split(','):
        t = TOPICS.get(d.strip())
        if t and t not in names:
            names.append(t)
    if not names:
        return 'A technical book about software development.'
    return 'A technical book about %s.' % (names[0] if len(names) == 1 else ', '.join(names[:-1]) + ' and ' + names[-1])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('headings')
    ap.add_argument('--out', required=True)
    ap.add_argument('--domains', default='ml,math')
    ap.add_argument('--map', action='append', default=[])
    ap.add_argument('--model', default='rosetta-ru')
    ap.add_argument('--url', default='http://ollama:11434')
    a = ap.parse_args()
    rules = [m.split('=', 1) for m in (a.map or ['Spring=programming,web'])]
    items = [json.loads(l) for l in open(a.headings, encoding='utf-8') if l.strip()]
    print('Заголовков: %d' % len(items), flush=True)
    import heading_context_test as h
    t0, changed = time.time(), 0
    with open(a.out, 'w', encoding='utf-8') as f:
        for k, it in enumerate(items):
            dom = next((d for part, d in rules if part in it['log']), a.domains)
            h.CONTEXT = topic_context(dom)
            it['ctx_c'] = h.CONTEXT
            it['c'] = call(a.url, a.model, system(it['terms'], None, bool(MARK.search(it['src']))), it['src'])
            changed += it['c'] != it['a']
            f.write(json.dumps({x: it[x] for x in ('log', 'n', 'src', 'terms', 'old', 'next', 'a', 'b', 'c', 'ctx_c') if x in it},
                               ensure_ascii=False) + '\n')
            if (k + 1) % 50 == 0:
                print('  %d/%d, %.1f с/заголовок, перевод изменился у %d' % (k + 1, len(items), (time.time() - t0) / (k + 1), changed), flush=True)
    print('Готово: %d заголовков, с темой книги перевод другой у %d → %s' % (len(items), changed, a.out))


if __name__ == '__main__':
    main()
