#!/usr/bin/env python3
"""Подсказка, какие области глоссария включить для книги, и какие частые однословные термины они навяжут.

Классификатор — сами глоссарии: для каждой области (config/glossary/<область>.csv, кроме base и keep) считается,
сколько РАЗНЫХ её терминов встречается в английском тексте книги. Термин ищется так же, как его ищет bbm:
целым словом, без учёта регистра (термин с заглавными — только точно), с английским множественным числом.
Модель и GPU не нужны; EPUB читается целиком, у PDF — выборка страниц.

  python detect_domains.py book.epub|book.pdf|text.txt [--dir /config/glossary] [--json out.json]
"""
import argparse, csv, json, os, re, sys, zipfile
from collections import Counter
from html.parser import HTMLParser

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from build_glossary import plurals  # noqa: E402

SKIP = {'pre', 'code', 'script', 'style', 'head', 'title', 'svg', 'math', 'var', 'sup', 'sub', 'nav'}
TOP_WORDS = 3            # сколько частых однословных терминов показывать у области
# Область предлагается по МНОГОСЛОВНЫМ терминам (cache miss, loss function): однословные (feature, node, commit)
# встречаются в любой книге в другом смысле. Порог подобран 30.09 на Java, Géron, AI Engineering, Pro Git,
# The API Book, testbook (EPUB и PDF): доля области среди найденных многословных терминов и их минимальное число.
MIN_SHARE, MIN_MULTI = 0.15, 3


class _Text(HTMLParser):
    def __init__(self):
        super().__init__()
        self.skip, self.out = 0, []

    def handle_starttag(self, t, a):
        if t in SKIP:
            self.skip += 1

    def handle_endtag(self, t):
        if t in SKIP and self.skip:
            self.skip -= 1

    def handle_data(self, d):
        if not self.skip:
            self.out.append(d)


def text_epub(path):
    z, parts = zipfile.ZipFile(path), []
    for n in z.namelist():
        if not n.endswith(('.xhtml', '.html', '.htm')) or os.path.basename(n).startswith('nav'):
            continue
        t = z.read(n).decode('utf-8', 'replace')
        if re.search(r'data-type="index"|epub:type="[^"]*\bindex\b', t):
            continue
        p = _Text()
        p.feed(t)
        parts.append(' '.join(p.out))
    return '\n'.join(parts)


def text_pdf(path, max_pages=150):
    import pdfplumber
    with pdfplumber.open(path) as pdf:
        n = len(pdf.pages)
        step = max(1, n // max_pages)
        parts = []
        for i in range(0, n, step):
            page = pdf.pages[i]
            parts.append(page.extract_text() or '')
            page.close()
    return '\n'.join(parts)


def book_text(path):
    ext = path.lower().rsplit('.', 1)[-1]
    if ext == 'epub':
        return text_epub(path)
    if ext == 'pdf':
        return text_pdf(path)
    return open(path, encoding='utf-8', errors='replace').read()


def read_domain(path):
    with open(path, encoding='utf-8-sig', newline='') as f:
        return [(r['source'].strip(), r['target'].strip()) for r in csv.DictReader(f)
                if (r.get('source') or '').strip() and not r['source'].startswith('#')]


def count(term, text, low):
    forms = [term] + plurals(term)
    n = 0
    for f in forms:
        if f == f.lower():
            n += len(re.findall(r'(?<!\w)' + re.escape(f) + r'(?!\w)', low))
        else:
            n += len(re.findall(r'(?<!\w)' + re.escape(f) + r'(?!\w)', text))
    return n


def detect(path, gdir):
    text = book_text(path)
    text = re.sub(r'\s+', ' ', text)
    low = text.lower()
    words = len(re.findall(r'[A-Za-z]{2,}', text))
    res = {}
    multi_total = 0
    for f in sorted(os.listdir(gdir)):
        if not f.endswith('.csv') or f in ('base.csv', 'keep.csv'):
            continue
        dom = f[:-4]
        hits = {}
        for s, t in read_domain(os.path.join(gdir, f)):
            c = count(s, text, low)
            if c:
                hits[s] = (c, t)
        single = sorted(((c, s, t) for s, (c, t) in hits.items() if ' ' not in s and '-' not in s), reverse=True)
        multi = sum(1 for s in hits if ' ' in s or '-' in s)
        multi_total += multi
        res[dom] = {'distinct': len(hits), 'multi': multi, 'total': len(read_domain(os.path.join(gdir, f))),
                    'occurrences': sum(c for c, _ in hits.values()),
                    'top_single': [{'term': s, 'count': c, 'target': t} for c, s, t in single[:TOP_WORDS]],
                    'top': sorted(hits, key=lambda s: -hits[s][0])[:8]}
    for x in res.values():
        x['share'] = round(x['multi'] / multi_total, 2) if multi_total else 0
        x['suggest'] = x['multi'] >= MIN_MULTI and x['share'] >= MIN_SHARE
    return {'words': words, 'domains': res, 'suggest': [d for d, x in res.items() if x['suggest']]}


if __name__ == '__main__':
    ap = argparse.ArgumentParser()
    ap.add_argument('book')
    ap.add_argument('--dir', default='/config/glossary')
    ap.add_argument('--json')
    a = ap.parse_args()
    r = detect(a.book, a.dir)
    if a.json:
        json.dump(r, open(a.json, 'w', encoding='utf-8'), ensure_ascii=False, indent=1)
    print('слов: %d; предлагаю: %s' % (r['words'], ', '.join(r['suggest']) or 'только base'))
    for d, x in r['domains'].items():
        print('  %s %-12s многословных %3d (доля %.2f), всего разных %3d; частые однословные: %s' % (
            '+' if x['suggest'] else ' ', d, x['multi'], x['share'], x['distinct'],
            ', '.join('%s %d → %s' % (t['term'], t['count'], t['target']) for t in x['top_single'])))
