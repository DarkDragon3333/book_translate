#!/usr/bin/env python3
"""Обратное преобразование prep_epub.py после перевода:
<var data-em="1"> → <em>, <var data-span="1"> → <span>, <var data-num="1">…</var> → содержимое.
Использование: python restore_epub.py translated.epub out.epub
"""
import re, sys, zipfile

VAR_EM = re.compile(r'<var((?:\s[^>]*?)?)\sdata-em="1"([^>]*)>(.*?)</var>', re.S)
VAR_SPAN = re.compile(r'<var((?:\s[^>]*?)?)\sdata-span="1"([^>]*)>(.*?)</var>', re.S)
HEAD_LABEL = re.compile(r'<(h[1-6])((?:\s[^>]*?)?)\sdata-label="([^"]*)"([^>]*)>')
VAR_NUM = re.compile(r'<var(?:\s[^>]*?)?\sdata-num="1"[^>]*>(.*?)</var>', re.S)
HTML = ('.html', '.xhtml', '.htm')


def restore(text, stats):
    def em(m):
        stats['em'] += 1
        return '<em%s%s>%s</em>' % (m.group(1), m.group(2), m.group(3))
    text = VAR_EM.sub(em, text)

    def span(m):
        stats['span'] += 1
        return '<span%s%s>%s</span>' % (m.group(1), m.group(2), m.group(3))
    text = VAR_SPAN.sub(span, text)
    text, n = VAR_NUM.subn(r'\1', text)
    stats['num'] += n

    def head(m):
        stats['head'] += 1
        return '<%s%s%s><span class="label">%s</span>' % (m.group(1), m.group(2), m.group(4), m.group(3).replace('&quot;', '"'))
    return HEAD_LABEL.sub(head, text)


def main(src, dst):
    zin, zout = zipfile.ZipFile(src), zipfile.ZipFile(dst, 'w')
    stats = {'em': 0, 'num': 0, 'span': 0, 'head': 0}
    for info in zin.infolist():
        data = zin.read(info.filename)
        if info.filename.endswith(HTML):
            text = data.decode('utf-8')
            if re.search(r'data-(em|num|span)="1"|data-label="', text):
                data = restore(text, stats).encode('utf-8')
        comp = zipfile.ZIP_STORED if info.filename == 'mimetype' else zipfile.ZIP_DEFLATED
        zout.writestr(info, data, compress_type=comp)
    zout.close()
    print('    Возвращено: курсив-переменные %d, подписи %d, номера заголовков %d, числовые ячейки %d'
          % (stats['em'], stats['span'], stats['head'], stats['num']))


if __name__ == '__main__':
    if len(sys.argv) != 3:
        sys.exit(__doc__)
    main(sys.argv[1], sys.argv[2])
