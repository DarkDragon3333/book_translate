#!/usr/bin/env python3
"""Оглавление переведённой книги — из переведённых заголовков; язык книги — ru.

bbm не переводит оглавление (nav.xhtml, toc.ncx): после перевода ссылки оглавления ведут на русские
заголовки, а подписи остаются английскими. Скрипт берёт текст заголовка, на который указывает ссылка
(файл#id; без id — первый заголовок файла), и подставляет его, если заголовок переведён (есть кириллица).
Заодно язык книги en → ru (dc:language и lang у страниц) — читалки переносят слова по-русски.

Использование: python fix_nav.py book_ru.epub      (файл меняется на месте)
"""
import html, posixpath, re, sys, zipfile

CYR = re.compile('[А-Яа-яЁё]')
HEAD = re.compile(r'<h([1-6])\b([^>]*)>(.*?)</h\1>', re.S)
NAV_TITLES = {'contents': 'Содержание', 'table of contents': 'Содержание'}


def text(frag):
    t = re.sub(r'<span[^>]*epub:type="pagebreak"[^>]*>\s*</span>', '', frag)
    return re.sub(r'\s+', ' ', html.unescape(re.sub(r'<[^>]+>', '', t))).strip()


def main(path):
    z = zipfile.ZipFile(path)
    files = {n: z.read(n) for n in z.namelist()}
    infos = {n: z.getinfo(n) for n in z.namelist()}
    z.close()
    opf = re.search(r'full-path="([^"]+)"', files['META-INF/container.xml'].decode()).group(1)
    base = posixpath.dirname(opf)
    o = files[opf].decode('utf-8')
    items = dict((m.group(2), m.group(1)) for m in re.finditer(r'<item\b[^>]*?href="([^"]+)"[^>]*?id="([^"]+)"', o))
    items.update((m.group(1), m.group(2)) for m in re.finditer(r'<item\b[^>]*?id="([^"]+)"[^>]*?href="([^"]+)"', o))
    full = lambda href, rel: posixpath.normpath(posixpath.join(posixpath.dirname(rel), href.split('#')[0]))

    heads = {}                                  # (файл, id | None) -> текст заголовка
    for n, data in files.items():
        if not n.endswith(('.xhtml', '.html', '.htm')):
            continue
        s = data.decode('utf-8', 'replace')
        first = None
        for m in HEAD.finditer(s):
            t = text(m.group(3))
            idm = re.search(r'\bid="([^"]+)"', m.group(2))
            if idm:
                heads[(n, idm.group(1))] = t
            if first is None and t:
                first = t
        for m in re.finditer(r'<(section|div)\b[^>]*\bid="([^"]+)"[^>]*>\s*(?:<[^h][^>]*>\s*)*<h([1-6])[^>]*>(.*?)</h\3>', s, re.S):
            heads.setdefault((n, m.group(2)), text(m.group(4)))    # ссылка на раздел, а не на заголовок
        heads[(n, None)] = first

    stats = {'nav': 0, 'ncx': 0, 'kept': 0}

    def lookup(href, rel):
        f = full(href, rel)
        frag = href.split('#', 1)[1] if '#' in href else None
        t = heads.get((f, frag)) if frag else heads.get((f, None))
        return t if t and CYR.search(t) else None

    for n in list(files):
        if not n.endswith(('.xhtml', '.html', '.ncx')):
            continue
        s = files[n].decode('utf-8', 'replace')
        if n.endswith('.ncx'):
            def ncx(m):
                t = lookup(m.group(3), n)
                if not t:
                    stats['kept'] += 1
                    return m.group(0)
                stats['ncx'] += 1
                return m.group(1) + html.escape(t, quote=False) + m.group(2) + m.group(3) + m.group(4)
            s2 = re.sub(r'(<navLabel>\s*<text>).*?(</text>\s*</navLabel>\s*<content\s+src=")([^"]+)(")', ncx, s, flags=re.S)
        elif 'epub:type="toc"' in s:
            def nav_block(mb):
                def a(m):
                    t = lookup(m.group(2), n)
                    if not t:
                        stats['kept'] += 1
                        return m.group(0)
                    stats['nav'] += 1
                    return m.group(1) + html.escape(t, quote=False) + m.group(3)
                blk = re.sub(r'(<a\b[^>]*\bhref="([^"]+)"[^>]*>).*?(</a>)', a, mb.group(0), flags=re.S)
                return re.sub(r'(<h[1-6][^>]*>)\s*([^<]+?)\s*(</h[1-6]>)',
                              lambda h: h.group(1) + NAV_TITLES.get(h.group(2).strip().lower(), h.group(2)) + h.group(3),
                              blk, count=1)
            s2 = re.sub(r'<nav\b[^>]*epub:type="toc".*?</nav>', nav_block, s, flags=re.S)
        else:
            s2 = s
        if n.endswith(('.xhtml', '.html')):
            s2 = re.sub(r'(<html\b[^>]*?\b(?:xml:)?lang=")en(?:-[A-Za-z]+)?(")', r'\1ru\2', s2)
            s2 = re.sub(r'(<html\b[^>]*?\b(?:xml:)?lang=")en(?:-[A-Za-z]+)?(")', r'\1ru\2', s2)
        if s2 != s:
            files[n] = s2.encode('utf-8')
    files[opf] = re.sub(r'(<dc:language[^>]*>)\s*en(?:-[A-Za-z]+)?\s*(</dc:language>)', r'\1ru\2', o).encode('utf-8')

    tmp = path + '.tmp'
    with zipfile.ZipFile(tmp, 'w') as out:
        if 'mimetype' in files:
            out.writestr(infos['mimetype'], files.pop('mimetype'), compress_type=zipfile.ZIP_STORED)
        for n, data in files.items():
            out.writestr(infos[n], data, compress_type=zipfile.ZIP_DEFLATED)
    import os
    os.replace(tmp, path)
    print('    Оглавление: переведено пунктов nav %d, ncx %d; оставлено как было %d'
          % (stats['nav'], stats['ncx'], stats['kept']))


if __name__ == '__main__':
    main(sys.argv[1])
