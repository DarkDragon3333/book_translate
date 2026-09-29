#!/usr/bin/env python3
"""Подготовка EPUB к переводу bbm.

1. Переменные в тексте (<em>θ</em>, <em>n</em>, <em>x</em>...) → <var data-em="1">.
   bbm стирает inline-разметку абзаца; var вместе с sub исключаются из перевода
   (--exclude-translate-tags) и возвращаются на место метками. Термины-курсивы
   (<em>gradient descent</em>) не трогаем — их надо переводить.
2. Подписи <span class="label">Figure 4-1. </span> → <var data-span="1">: остаются
   английскими, а прочие span (keep-together) переводятся вместе с текстом.
   В заголовках номер (<h2><span class="label">A.2 </span>…) уходит в атрибут data-label: из заголовков
   bbm вырезает любую разметку, и без этого модель переделывала «A.2» в «2.2», «A.4» — в «Глава 4.».
3. Ячейки/абзацы из одних чисел (<td><p>0</p></td>) → <var data-num="1">,
   иначе модель пишет «ноль», «двое».
Обратное преобразование — restore_epub.py. Nav и предметный указатель не трогаем.

Использование:
  python prep_epub.py in.epub out.epub
  python prep_epub.py --index-files book.epub   # файлы указателя через запятую (для --exclude_filelist)
"""
import posixpath, re, sys, zipfile

EM = re.compile(r'<em(\s[^>]*)?>([^<]*)</em>')
LABEL = re.compile(r'<span(\s[^>]*\bclass="label"[^>]*)>([^<]*)</span>')
HEAD_LABEL = re.compile(r'<(h[1-6])((?:\s[^>]*)?)>(\s*)<span\s[^>]*\bclass="label"[^>]*>([^<]*)</span>\s*')
NUM_BLOCK = re.compile(r'<(p|td|th)(\s[^>]*)?>(\s*)([^<]*?)(\s*)</\1>')
NUMERIC = re.compile(r'^[\d\s.,:;%±≥≤<>=+\-–—−×/()]+$')
HTML = ('.html', '.xhtml', '.htm')


def is_variable(text):
    t = text.strip()
    return bool(t) and (len(t) <= 3 or not re.search(r'[A-Za-z]{4}', t))


INDEX = re.compile(r'<(?:section|body|div)\b[^>]*(?:data-type="index"|epub:type="[^"]*\bindex\b)')


def is_index(text):
    return INDEX.search(text) is not None


def skip(text):
    return '<nav' in text or is_index(text)


def convert(text, stats):
    def em(m):
        if not is_variable(m.group(2)):
            return m.group(0)
        stats['em→var'] += 1
        return '<var data-em="1"%s>%s</var>' % (m.group(1) or '', m.group(2))

    def num(m):
        tag, attrs, pre, body, post = m.groups()
        if not body or not NUMERIC.match(body) or not re.search(r'\d', body):
            return m.group(0)
        stats['числа'] += 1
        return '<%s%s>%s<var data-num="1">%s</var>%s</%s>' % (tag, attrs or '', pre, body, post, tag)

    def label(m):
        stats['подписи'] += 1
        return '<var data-span="1"%s>%s</var>' % (m.group(1), m.group(2))

    def head(m):
        stats['номера заголовков'] += 1
        return '<%s%s data-label="%s">%s' % (m.group(1), m.group(2), m.group(4).replace('"', '&quot;'), m.group(3))

    text = EM.sub(em, text)
    text = HEAD_LABEL.sub(head, text)
    text = LABEL.sub(label, text)
    return NUM_BLOCK.sub(num, text)


def opf_dir(z):
    cont = z.read('META-INF/container.xml').decode('utf-8')
    opf = re.search(r'full-path="([^"]+)"', cont).group(1)
    return posixpath.dirname(opf)


def index_files(path):
    z = zipfile.ZipFile(path)
    base = opf_dir(z)
    names = []
    for n in z.namelist():
        if n.endswith(HTML) and is_index(z.read(n).decode('utf-8', 'replace')):
            names.append(posixpath.relpath(n, base) if base else n)
    return names


def main(src, dst):
    zin, zout = zipfile.ZipFile(src), zipfile.ZipFile(dst, 'w')
    stats = {'em→var': 0, 'числа': 0, 'подписи': 0, 'номера заголовков': 0}
    for info in zin.infolist():
        data = zin.read(info.filename)
        if info.filename.endswith(HTML):
            text = data.decode('utf-8')
            if re.search(r'data-(em|num|span)="1"', text):
                sys.exit('Книга уже подготовлена (есть data-em/data-num): %s' % info.filename)
            if not skip(text):
                data = convert(text, stats).encode('utf-8')
        comp = zipfile.ZIP_STORED if info.filename == 'mimetype' else zipfile.ZIP_DEFLATED
        zout.writestr(info, data, compress_type=comp)
    zout.close()
    print('    Переменные em→var: %d, подписи span.label: %d, номера заголовков: %d, числовые ячейки: %d'
          % (stats['em→var'], stats['подписи'], stats['номера заголовков'], stats['числа']))


if __name__ == '__main__':
    if len(sys.argv) == 3 and sys.argv[1] == '--index-files':
        print(','.join(index_files(sys.argv[2])))
    elif len(sys.argv) == 3:
        main(sys.argv[1], sys.argv[2])
    else:
        sys.exit(__doc__)
