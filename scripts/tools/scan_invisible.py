#!/usr/bin/env python3
"""Невидимые символы и виды сносок в книгах корпуса (14.10).

Повод: O'Reilly ставит U+2060 (WORD JOINER) перед номером сноски, и Rosetta теряла такую метку в 27% случаев
(остальные метки — ~1%). Адаптер теперь убирает все невидимые символы (категория Unicode Cf) перед отправкой
модели и возвращает их перед метками. Скрипт показывает, какие такие символы реально встречаются в EPUB корпуса,
перед чем стоят и как издательства оформляют сноски — чтобы ничего не пропустить.

  python scan_invisible.py /books/corpus_src [ещё папки или .epub …] [--out отчёт.txt]
"""
import argparse, collections, os, re, sys, unicodedata, zipfile

HTML = ('.html', '.xhtml', '.htm')
SPACES = {' ': 'NO-BREAK SPACE', ' ': 'NARROW NO-BREAK SPACE', ' ': 'THIN SPACE',
          ' ': 'HAIR SPACE', ' ': 'FIGURE SPACE', ' ': 'EN SPACE', ' ': 'EM SPACE'}
SUPDIG = '¹²³⁴⁵⁶⁷⁸⁹⁰'
NOTEREF = re.compile(r'<a\b[^>]*(?:epub:type="[^"]*noteref|role="doc-noteref")[^>]*>', re.I)
SUP = re.compile(r'<sup\b[^>]*>', re.I)
ENT = re.compile(r'&#(x[0-9a-fA-F]+|\d+);')


def chars_of_interest(c):
    return unicodedata.category(c) == 'Cf' or c in SPACES


def decode_entities(t):
    def one(m):
        v = m.group(1)
        cp = int(v[1:], 16) if v[0] in 'xX' else int(v)
        try:
            c = chr(cp)
        except ValueError:
            return m.group(0)
        return c if chars_of_interest(c) else m.group(0)
    return ENT.sub(one, t)


def context(t, i):
    """Что стоит сразу после символа: сноска <sup>/noteref, другой тег или текст."""
    rest = t[i + 1:i + 200]
    if SUP.match(rest) or NOTEREF.match(rest) or re.match(r'<a\b[^>]*>\s*<sup', rest, re.I):
        return 'перед сноской'
    if rest.startswith('<'):
        return 'перед тегом'
    if not rest or rest[0].isspace():
        return 'перед пробелом/концом'
    return 'в тексте'


def books(paths):
    for p in paths:
        if os.path.isdir(p):
            for root, _, files in os.walk(p):
                for f in sorted(files):
                    if f.lower().endswith('.epub'):
                        yield os.path.join(root, f)
        elif p.lower().endswith('.epub'):
            yield p


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('paths', nargs='+')
    ap.add_argument('--out')
    a = ap.parse_args()
    cnt, ctx, nbooks, ex = collections.Counter(), collections.defaultdict(collections.Counter), \
        collections.defaultdict(set), {}
    notes, notes_books = collections.Counter(), collections.defaultdict(set)
    n = 0
    for path in books(a.paths):
        name = os.path.basename(path)
        try:
            z = zipfile.ZipFile(path)
        except Exception as e:
            print('  пропущено %s: %s' % (name, e), file=sys.stderr)
            continue
        n += 1
        for f in z.namelist():
            if not f.lower().endswith(HTML):
                continue
            t = decode_entities(z.read(f).decode('utf-8', 'replace'))
            for i, c in enumerate(t):
                if chars_of_interest(c):
                    cnt[c] += 1
                    ctx[c][context(t, i)] += 1
                    nbooks[c].add(name)
                    if c not in ex:
                        ex[c] = (name, re.sub(r'[ \t\r\n]+', ' ', t[max(0, i - 60):i + 60].replace(c, '‹U+%04X›' % ord(c))))
            for kind, rx in (('<sup> (номер сноски или степень)', SUP), ('<a noteref> (сноска EPUB3)', NOTEREF)):
                k = len(rx.findall(t))
                if k:
                    notes[kind] += k
                    notes_books[kind].add(name)
            body = re.sub(r'<[^>]+>', ' ', t)
            for kind, rx in (('надстрочные цифры ¹²³ в тексте', re.compile('[%s]' % SUPDIG)),
                             ('† ‡ § в тексте', re.compile('[†‡§]'))):
                k = len(rx.findall(body))
                if k:
                    notes[kind] += k
                    notes_books[kind].add(name)
        if n % 25 == 0:
            print('  книг: %d' % n, file=sys.stderr, flush=True)
    out = ['Книг EPUB: %d' % n, '', 'Невидимые символы и особые пробелы:']
    if not cnt:
        out.append('  нет')
    for c, k in cnt.most_common():
        nm = unicodedata.name(c, SPACES.get(c, '?'))
        out.append('  U+%04X %-24s %7d раз, книг %3d; %s' % (ord(c), nm, k, len(nbooks[c]),
                   ', '.join('%s %d' % x for x in ctx[c].most_common())))
        out.append('      пример (%s): …%s…' % ex[c])
    out += ['', 'Оформление сносок:']
    for kind, k in notes.most_common():
        out.append('  %-35s %7d, книг %3d' % (kind, k, len(notes_books[kind])))
    text = '\n'.join(out)
    if a.out:
        with open(a.out, 'w', encoding='utf-8') as f:
            f.write(text + '\n')
        print('Записано: %s' % a.out)
    print(text)


if __name__ == '__main__':
    main()
