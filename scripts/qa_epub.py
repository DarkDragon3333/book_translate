#!/usr/bin/env python3
"""Сверка переведённого EPUB с оригиналом (структура, не текст).
Использование: python qa_epub.py original.epub translated.epub
Проверяет по главам: pre / math / code / ul / ol / li / dl / table / blockquote / noteref / img / sub / sup,
перекрёстные ссылки (xref), внешние ссылки (http), подписи (span.label),
выделение (em / strong — bbm его не сохраняет, выводится отдельной строкой),
совпадение содержимого pre, math, code (без учёта U+200B/U+2060/пробелов),
остатки меток {{id_N}} и ⟦...⟧, китайские символы, английские абзацы вне кода (по файлам;
служебные пункты оглавления — nav landmarks / page-list — не переводятся и не считаются).
Файлы, порезанные Calibre (*_split_NNN.html), склеиваются обратно.
"""
import re, sys, zipfile, os
from collections import Counter
from bs4 import BeautifulSoup

N = lambda s: re.sub(r'[​⁠\s]', '', s)
TAGS = ['pre', 'math', 'code', 'ul', 'ol', 'li', 'dl', 'table', 'blockquote', 'img', 'sub', 'sup']
EMPH = ['em', 'strong']            # выделение: bbm стирает его в прозе, это известное ограничение

def load(path):
    z = zipfile.ZipFile(path); out = {}
    for n in sorted(z.namelist()):
        if n.endswith(('.html', '.xhtml')):
            key = re.sub(r'_split_\d+', '', os.path.basename(n))
            out[key] = out.get(key, '') + z.read(n).decode('utf-8')
    return out

def stats(s):
    c = Counter({t: len(s.find_all(t)) for t in TAGS + EMPH})
    c['noteref'] = len([a for a in s.find_all('a')          # O'Reilly: data-type, pandoc: epub:type
                        if a.get('data-type') == 'noteref' or 'noteref' in (a.get('epub:type') or '')])
    c['xref'] = len(s.find_all('a', attrs={'data-type': 'xref'}))
    c['http'] = len([a for a in s.find_all('a') if (a.get('href') or '').startswith('http')])
    c['label'] = len(s.find_all('span', class_='label'))
    return c

def main(orig, tran):
    o_files, t_files = load(orig), load(tran)
    tot_o, tot_t, problems, eng, lost_emph = Counter(), Counter(), [], Counter(), []
    for name, html in o_files.items():
        if name not in t_files:
            problems.append(f'{name}: нет в переводе'); continue
        o = BeautifulSoup(html, 'html.parser'); t = BeautifulSoup(t_files[name], 'html.parser')
        so, st = stats(o), stats(t); tot_o += so; tot_t += st
        msgs = [f'{k} {so[k]}→{st[k]}' for k in so if so[k] != st[k] and k not in EMPH]
        emph = [f'{k} {so[k]}→{st[k]}' for k in EMPH if so[k] != st[k]]
        for tag in ('pre', 'math', 'code'):
            mo = Counter(N(x.get_text()) for x in o.find_all(tag))
            mt = Counter(N(x.get_text()) for x in t.find_all(tag))
            lost = sum((mo - mt).values())
            if lost: msgs.append(f'{tag}: изменено/потеряно {lost}')
        txt = t.get_text()
        ph = len(re.findall(r'\{\{id_\d+\}\}', txt)) + txt.count('⟦')
        cjk = len(re.findall(r'[一-鿿぀-ヿ]', txt))
        if ph: msgs.append(f'остатки меток: {ph}')
        if cjk: msgs.append(f'китайские/японские символы: {cjk}')
        if msgs: problems.append(f'{name}: ' + '; '.join(msgs))
        if emph: lost_emph.append(f'{name}: ' + ', '.join(emph))
        if 'data-type="index"' in t_files[name]:
            continue
        for x in t.find_all(['pre', 'code', 'math']): x.decompose()
        for x in t.find_all('nav'):             # служебные пункты оглавления (Title Page, Cover…) не переводятся
            if x.has_attr('hidden') or re.search(r'landmarks|page-list', x.get('epub:type') or ''):
                x.decompose()
        for e in t.find_all(['p', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'li', 'figcaption', 'td', 'th', 'dt', 'dd']):
            if e.find(['p', 'li', 'div']): continue
            tx = e.get_text(' ', strip=True)
            if len(tx) > 15 and not re.search('[А-Яа-яЁё]', tx) and re.search('[A-Za-z]{3}', tx):
                par = e.find_parent(attrs={'data-type': True})
                eng[name] += 1
    print('ИТОГО  оригинал:', dict(tot_o)); print('ИТОГО  перевод: ', dict(tot_t))
    print('\nРасхождения по файлам:' if problems else '\nРасхождений нет.')
    for p in problems: print('  ' + p)
    if lost_emph:
        print('\nПотеряно выделение (курсив, жирный — ограничение bbm, текст при этом переведён):')
        for p in lost_emph: print('  ' + p)
    print('\nНепереведённые (английские) абзацы вне кода по файлам:')
    if not eng:
        print('  нет')
    for n, c in sorted(eng.items()):
        print('  %s: %d' % (n, c))

if __name__ == '__main__':
    main(sys.argv[1], sys.argv[2])
