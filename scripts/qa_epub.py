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
С 09.10 (OCP): буквы вариантов ответа, код в заголовках, склейка кода с русским словом,
код, перенесённый в конец абзаца (bbm дописывает туда потерянные метки — счётчики при этом равны).
Использование: python qa_epub.py original.epub translated.epub [--only c03.xhtml,c04.xhtml]
"""
import re, sys, zipfile, os
from collections import Counter
from bs4 import BeautifulSoup
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from prep_epub import is_code_word, italic_code_book, INDEX

ANS = re.compile(r'^\s*([A-J](?:, [A-J])*)\.\s')
CYR_OPT = re.compile(r'[Вв]ариант\w*\s+[А-ЕЖЗИ](?![\w])')
GLUED = re.compile(r'</code>[А-Яа-яЁё]|[А-Яа-яЁё]<code\b')
HEADS = ['h1', 'h2', 'h3', 'h4', 'h5', 'h6']


def last_child(e):
    """Последний значимый узел элемента (пробелы и пустые span-метки страниц не в счёт)."""
    for c in reversed(list(e.children)):
        if getattr(c, 'name', None) is None:
            if str(c).strip():
                return c
            continue
        if c.name == 'span' and not c.get_text(strip=True):
            continue
        return c
    return None


def extra_checks(o, t, name, ex, italic_code):
    tid = {e['id']: e for e in t.find_all(True, id=True)}
    for e in o.find_all(['p', 'li'] + HEADS, id=True):
        te = tid.get(e['id'])
        if te is None:
            continue
        if e.name in HEADS:
            for x in e.find_all(['i', 'em', 'code']):
                w = x.get_text().strip()
                if '<' in w or '>' in w:
                    continue
                if (x.name == 'code' or is_code_word(w, heading=True, italic_code=italic_code)) and w not in te.get_text():
                    ex['код в заголовке переведён'].append(f'{name}: «{e.get_text(" ", strip=True)}» → «{te.get_text(" ", strip=True)}»')
            continue
        m = ANS.match(e.get_text())
        if m and not re.match(re.escape(m.group(1)) + r'\.\s', te.get_text().lstrip()):
            ex['буква ответа изменена или склеена'].append(f'{name}#{e["id"]}: {m.group(1)} → «{te.get_text()[:40].strip()}…»')
        lo, lt = last_child(e), last_child(te)
        # bbm дописывает потерянный код в конец абзаца — после точки: «…без проблем. hunt() getName()».
        # Код в конце обычной фразы («в байт-код .class») — нормальный порядок слов, не считаем.
        before = lt.previous_sibling if getattr(lt, 'name', None) == 'code' else None
        while before is not None and getattr(before, 'name', None) == 'code' or (before is not None and
                getattr(before, 'name', None) is None and not str(before).strip()):
            before = before.previous_sibling
        if (getattr(lt, 'name', None) == 'code' and getattr(lo, 'name', None) != 'code' and e.find('code')
                and not e.find(['ol', 'ul', 'pre']) and before is not None
                and re.search(r'[.!?:;»)]\s*$', before if isinstance(before, str) else before.get_text())):
            ex['код перенесён в конец абзаца'].append(f'{name}#{e["id"]}: …{te.get_text()[-60:].strip()}')

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

def main(orig, tran, only=None):
    o_files, t_files = load(orig), load(tran)
    tot_o, tot_t, problems, eng, lost_emph = Counter(), Counter(), [], Counter(), []
    ex = {'буква ответа изменена или склеена': [], 'код в заголовке переведён': [], 'код перенесён в конец абзаца': []}
    cyr_opt = glued = 0
    italic_code = italic_code_book(o_files.values())
    for name, html in o_files.items():
        if only and name not in only:
            continue
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
        if INDEX.search(t_files[name]):         # предметный указатель не переводится
            continue
        extra_checks(o, t, name, ex, italic_code)
        cyr_opt += len(CYR_OPT.findall(txt))
        glued += len(GLUED.findall(t_files[name]))
        for x in t.find_all(['pre', 'code', 'math']): x.decompose()
        for x in t.find_all('nav'):             # служебные пункты оглавления (Title Page, Cover…) не переводятся
            if x.has_attr('hidden') or re.search(r'landmarks|page-list', x.get('epub:type') or ''):
                x.decompose()
        for x in t.find_all('a', href=True):    # номера страниц в оглавлении («814») — не текст
            if re.fullmatch(r'[\divxlcm]+', x.get_text(strip=True), re.I):
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
    print('\nКнига-экзамен и код:')
    for k, v in ex.items():
        print('  %s: %d' % (k, len(v)))
        for line in v[:15]:
            print('    ' + line)
        if len(v) > 15:
            print('    … ещё %d' % (len(v) - 15))
    print('  «вариант» с русской буквой (Б, В…): %d' % cyr_opt)
    print('  код склеен с русским словом: %d' % glued)

if __name__ == '__main__':
    a = sys.argv[1:]
    only = None
    if '--only' in a:
        i = a.index('--only'); only = {os.path.basename(x) for x in a[i + 1].split(',') if x}; del a[i:i + 2]
    main(a[0], a[1], only)
