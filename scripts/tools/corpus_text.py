#!/usr/bin/env python3
"""Выгрузка текста книг в корпус для анализа терминов (глоссарий, подбор областей, смысл слов, пары EN↔RU).

Берёт все .epub и .pdf из папки (рекурсивно), пишет по файлу <папка вывода>/<имя книги>.txt:
  - EPUB: абзацы по порядку чтения, без кода (pre/code), формул и указателя; перед каждой главой строка «### ГЛАВА n».
  - PDF: текст страниц без символов шрифта кода (шрифт кода — как в pdf_analyze.py) и без рекламы t.me;
    перед каждой страницей строка «### СТР n».
И manifest.json: язык (доля кириллицы), страниц/глав, слов, признак скана (страницы без текста).
Готовые файлы не пересчитываются (если книга не менялась). Текст книг остаётся в books/ и в git не попадает.

  python corpus_text.py /books/corpus_src /books/.corpus
"""
import json, logging, os, re, signal, sys, time, urllib.parse, zipfile
from html.parser import HTMLParser

logging.getLogger('pdfminer').setLevel(logging.ERROR)     # «Could not get FontBBox…» — шум, на текст не влияет
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import pdf_analyze  # noqa: E402

SKIP = {'pre', 'code', 'script', 'style', 'head', 'title', 'svg', 'math', 'nav'}
BLOCK = {'p', 'li', 'dd', 'dt', 'figcaption', 'blockquote', 'td', 'th', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'div'}
AD = re.compile(r'(\bt\.me/|telegram|телеграм|\bvk\.com/)', re.I)


class Paras(HTMLParser):
    def __init__(self):
        super().__init__()
        self.skip, self.cur, self.out = 0, [], []

    def handle_starttag(self, t, a):
        if t in SKIP:
            self.skip += 1
        if t in BLOCK:
            self.flush()

    def handle_endtag(self, t):
        if t in SKIP and self.skip:
            self.skip -= 1
        if t in BLOCK:
            self.flush()

    def handle_data(self, d):
        if not self.skip:
            self.cur.append(d)

    def flush(self):
        t = re.sub(r'\s+', ' ', ''.join(self.cur)).strip()
        if t:
            self.out.append(t)
        self.cur = []


def epub_text(path):
    z = zipfile.ZipFile(path)
    opf = re.search(r'full-path="([^"]+)"', z.read('META-INF/container.xml').decode('utf-8', 'replace')).group(1)
    o = z.read(opf).decode('utf-8', 'replace')
    base = os.path.dirname(opf)
    items = {}
    for m in re.finditer(r'<item\b([^>]*)/?>', o):
        a = dict(re.findall(r'([\w:-]+)="([^"]*)"', m.group(1)))
        items[a.get('id')] = a
    lines, n = [], 0
    for idref in re.findall(r'<itemref\b[^>]*idref="([^"]+)"', o):
        a = items.get(idref) or {}
        if not a.get('href') or 'nav' in (a.get('properties') or ''):
            continue
        href = urllib.parse.unquote(a['href'].split('#')[0])      # «ch%2001.xhtml#top» → «ch 01.xhtml»
        name = os.path.normpath(os.path.join(base, href)).replace('\\', '/')
        try:
            t = z.read(name).decode('utf-8', 'replace')
        except KeyError:
            low = {n.lower(): n for n in z.namelist()}               # регистр имени в манифесте и в архиве не совпал
            if name.lower() not in low:
                continue
            t = z.read(low[name.lower()]).decode('utf-8', 'replace')
        if re.search(r'data-type="index"|epub:type="[^"]*\bindex\b', t):
            continue
        p = Paras()
        p.feed(t)
        p.flush()
        if p.out:
            n += 1
            lines.append('### ГЛАВА %d' % n)
            lines += [x for x in p.out if not AD.search(x)]
    return lines, {'chapters': n}


PAGE_LIMIT = 20          # с на страницу: дольше — страница пропускается (тяжёлые векторные схемы)
FONT_LIMIT = 90          # с на поиск шрифта кода
BOOK_LIMIT = 20 * 60     # с на книгу: дольше — остаток книги пропускается, книга помечается «частично»


class _Slow(Exception):
    pass


_armed = [False]


def _alarm(*_):
    if _armed[0]:                # таймер сработал, пока страница ещё разбирается
        raise _Slow()


def pdf_text(path):
    import pdfplumber
    print('    шрифт кода…', flush=True)
    signal.signal(signal.SIGALRM, _alarm)
    try:                                 # выборка страниц для шрифта кода тоже может застрять на тяжёлой странице
        _armed[0] = True
        signal.setitimer(signal.ITIMER_REAL, FONT_LIMIT)
        try:
            code = pdf_analyze.analyze(path, fonts_only=True)['code_font_pattern']
        finally:
            _armed[0] = False
            signal.setitimer(signal.ITIMER_REAL, 0)
    except Exception:
        code = ''
        print('    шрифт кода не определён за %d с — код останется в тексте' % FONT_LIMIT, flush=True)
    code_re = re.compile(code) if code else None
    lines, empty, slow, partial = [], 0, [], False
    t0 = time.time()
    signal.signal(signal.SIGALRM, _alarm)
    with pdfplumber.open(path) as pdf:
        n = len(pdf.pages)
        print('    страниц %d' % n, flush=True)
        for i, page in enumerate(pdf.pages):
            if time.time() - t0 > BOOK_LIMIT:
                partial = True
                print('    остановлено на стр. %d из %d: книга дольше %d мин' % (i + 1, n, BOOK_LIMIT // 60), flush=True)
                break
            if i and i % 100 == 0:
                print('    стр. %d из %d, %d с' % (i, n, time.time() - t0), flush=True)
            try:
                _armed[0] = True
                signal.setitimer(signal.ITIMER_REAL, PAGE_LIMIT)
                try:
                    if code_re:
                        page = page.filter(lambda o: not (o.get('object_type') == 'char' and code_re.search(o.get('fontname', ''))))
                    t = page.extract_text(x_tolerance_ratio=0.15) or ''   # относительный допуск: не склеивает слова
                finally:
                    _armed[0] = False
                    signal.setitimer(signal.ITIMER_REAL, 0)
            except Exception:            # медленная (pdfplumber заворачивает таймер в своё исключение) или битая страница
                slow.append(i + 1)
                t = ''
            if len(t.strip()) < 20:
                empty += 1
            lines.append('### СТР %d' % (i + 1))
            lines += [l for l in t.splitlines() if l.strip() and not AD.search(l)]
            if hasattr(page, 'close'):
                page.close()
    if slow:
        print('    пропущены страницы (медленные или битые): %s' % ', '.join(map(str, slow[:20])), flush=True)
    return lines, {'pages': n, 'empty_pages': empty, 'code_font': code, 'scan': empty > 0.2 * n and not slow,
                   'slow_pages': slow, 'partial': partial}


def main(src, dst):
    os.makedirs(dst, exist_ok=True)
    mpath = os.path.join(dst, 'manifest.json')
    try:
        manifest = json.load(open(mpath, encoding='utf-8'))
    except (OSError, ValueError):
        manifest = {}
    books = []
    for root, _, files in os.walk(src):
        for f in files:
            if f.lower().endswith(('.epub', '.pdf')):
                books.append(os.path.join(root, f))
    books.sort()
    print('Книг: %d' % len(books), flush=True)
    for k, path in enumerate(books, 1):
        name = os.path.basename(path)
        out = os.path.join(dst, name + '.txt')
        st = os.stat(path)
        key = [st.st_size, int(st.st_mtime)]
        prev = manifest.get(name, {})
        if os.path.isfile(out) and prev.get('key') == key and prev.get('words', 0) >= 1000:   # почти пустые — пересчитать
            print('[%d/%d] %s — уже есть' % (k, len(books), name), flush=True)
            continue
        print('[%d/%d] %s …' % (k, len(books), name), flush=True)
        t0 = time.time()
        try:
            lines, info = (epub_text if name.lower().endswith('.epub') else pdf_text)(path)
        except Exception as e:
            print('[%d/%d] %s — ОШИБКА: %s' % (k, len(books), name, e), flush=True)
            manifest[name] = {'key': key, 'error': str(e)}
            continue
        text = '\n'.join(lines)
        with open(out, 'w', encoding='utf-8') as f:
            f.write(text)
        cyr = len(re.findall('[А-Яа-яЁё]', text))
        lat = len(re.findall('[A-Za-z]', text))
        info.update({'key': key, 'words': len(re.findall(r'\w+', text)),
                     'lang': 'ru' if cyr > lat else 'en', 'seconds': round(time.time() - t0)})
        manifest[name] = info
        json.dump(manifest, open(mpath, 'w', encoding='utf-8'), ensure_ascii=False, indent=1)
        print('[%d/%d] %s — %s, слов %d, %d с%s%s' % (k, len(books), name, info['lang'], info['words'], info['seconds'],
              ', ПОХОЖЕ НА СКАН' if info.get('scan') else '', ', ЧАСТИЧНО' if info.get('partial') else ''), flush=True)
    json.dump(manifest, open(mpath, 'w', encoding='utf-8'), ensure_ascii=False, indent=1)
    print('Готово: %s' % dst)


if __name__ == '__main__':
    if len(sys.argv) != 3:
        sys.exit(__doc__)
    main(sys.argv[1], sys.argv[2])
