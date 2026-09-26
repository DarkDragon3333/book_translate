#!/usr/bin/env python3
"""Выравнивает термины в переведённом EPUB — только проза; pre/code/math/var не трогаются.
Исправляет систематические ошибки модели, которые не лечит глоссарий (с учётом падежей).
Текст ссылок и подписей («Figure 4-1», «Equation 4-1.») намеренно остаётся английским.
Использование: python normalize_terms.py in.epub out.epub
Правила — в RULES; пополнять по отчётам после каждой книги.
"""
import re, sys, zipfile
from collections import Counter

FIG = {'а': 'ок', 'е': 'ке', 'ы': 'ка', 'у': 'ок', 'ой': 'ком'}


def fig(m):
    first, end, num = m.group(1), m.group(2), m.group(3)
    return ('Р' if first == 'Ф' else 'р') + 'исун' + FIG[end] + ' ' + num


def normal_eq(m):
    prep, form = m.group(1) or '', m.group(3)
    table = {'а': 'нормальное уравнение', 'у': 'нормальное уравнение',
             'ы': 'нормального уравнения', 'ой': 'нормальным уравнением',
             'е': 'нормальном уравнении' if prep.strip().lower() in ('в', 'о', 'об', 'при') else 'нормальному уравнению'}
    res = table[form]
    if m.group(2)[0].isupper():
        res = res[0].upper() + res[1:]
    return prep + res


def batch_gd(m):
    return 'пакетн' + m.group(1) + ' градиентн' + m.group(1) + ' ' + m.group(2)


RULES = [
    ('Фигура N → Рисунок N', re.compile(r'\b([Фф])игур(а|е|ы|у|ой)\s+(\d+[-–.]\d+)'), fig),
    ('функция затрат → функция стоимости', re.compile(r'(функци\w*)\s+затрат\b'), r'\1 стоимости'),
    ('нормальная формула → нормальное уравнение',
     re.compile(r'(\b(?:в|о|об|при)\s+)?\b([Нн]ормальн\w*)\s+формул(а|ы|е|у|ой)\b'), normal_eq),
    ('градиентный спуск с полным набором → пакетный',
     re.compile(r'градиентн(ый|ого|ому|ым|ом)\s+(спуск\w*)\s+с\s+полным\s+набором'), batch_gd),
    ('(Рисунок N → (рисунок N', re.compile(r'\((Уравнени|Рисун|Глав)'), lambda m: '(' + m.group(1).lower()),
]
HEADING_FIX = {'Заметка': 'Примечание', 'Замечание': 'Примечание', 'Внимание': 'Предупреждение'}

# Глоссарий даёт термин со строчной буквы, и модель вставляет его в начало заголовка как есть
# («нормальное уравнение»). Делаем первую букву заголовка/подписи заглавной.
CAP = re.compile(r'(<(h[1-6]|caption|th|td|dt)\b[^>]*>(?:\s*(?:<p\b[^>]*>|<span\b[^>]*class="label"[^>]*>[^<]*</span>|<a\b[^>]*>\s*</a>))*\s*)([а-яё])')
SKIP = re.compile(r'<(/?)(pre|code|math|var)\b[^>]*?(/?)>', re.I)
TOKEN = re.compile(r'(<[^>]+>)')


def process(html, stats):
    parts = TOKEN.split(html)
    depth = 0
    for i, p in enumerate(parts):
        if p.startswith('<'):
            m = SKIP.match(p)
            if m and not m.group(3):
                depth += -1 if m.group(1) else 1
            continue
        if depth or not p.strip():
            continue
        s = p
        if s.strip() in HEADING_FIX and i >= 1 and re.match(r'<h\d', parts[i - 1] or ''):
            new = HEADING_FIX[s.strip()]
            stats[f'{s.strip()} → {new} (заголовки)'] += 1
            s = s.replace(s.strip(), new)
        for name, rx, repl in RULES:
            s, n = rx.subn(repl, s)
            stats[name] += n
        parts[i] = s
    html = ''.join(parts)
    html, n = CAP.subn(lambda m: m.group(1) + m.group(3).upper(), html)
    stats['заглавная буква в заголовках'] += n
    return html


def main(src, dst):
    zin, zout = zipfile.ZipFile(src), zipfile.ZipFile(dst, 'w')
    stats = Counter()
    for info in zin.infolist():
        data = zin.read(info.filename)
        if info.filename.endswith(('.html', '.xhtml', '.htm', '.ncx')):
            data = process(data.decode('utf-8'), stats).encode('utf-8')
        comp = zipfile.ZIP_STORED if info.filename == 'mimetype' else zipfile.ZIP_DEFLATED
        zout.writestr(info, data, compress_type=comp)
    zout.close()
    for k, v in stats.items():
        if v:
            print(f'    {k}: {v}')


if __name__ == '__main__':
    if len(sys.argv) != 3:
        sys.exit(__doc__)
    main(sys.argv[1], sys.argv[2])
