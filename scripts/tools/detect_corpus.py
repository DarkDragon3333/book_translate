#!/usr/bin/env python3
"""Проверка подбора областей (detect_domains) на корпусе, разложенном по папкам тем.

Для каждой книги (*.txt в подпапках и в самой папке) считает области так же, как интерфейс, и печатает по каждой папке-теме,
сколько книг получили каждую область. Текст книги обрезается до --max-chars (по умолчанию 600 тыс. символов ≈ 100 тыс. слов).
Результат по книгам — CSV (только имена файлов и числа).

  python detect_corpus.py /books/.corpus2 --out /books/detect_corpus.csv [--jobs 8]
"""
import argparse, collections, csv, os, sys
from multiprocessing import Pool

sys.path.insert(0, '/scripts')
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))
import detect_domains as dd  # noqa: E402

GDIR, MAXC = '/config/glossary', 600000


def one(path):
    text = open(path, encoding='utf-8', errors='replace').read()[:MAXC]
    try:
        orig = dd.book_text
        dd.book_text = lambda p: text
        r = dd.detect(path, GDIR)
    finally:
        dd.book_text = orig
    return path, r


def main():
    global GDIR, MAXC
    ap = argparse.ArgumentParser()
    ap.add_argument('root')
    ap.add_argument('--glossary', default='/config/glossary')
    ap.add_argument('--max-chars', type=int, default=600000)
    ap.add_argument('--jobs', type=int, default=8)
    ap.add_argument('--out', required=True)
    a = ap.parse_args()
    GDIR, MAXC = a.glossary, a.max_chars
    files = [os.path.join(a.root, f) for f in sorted(os.listdir(a.root)) if f.endswith('.txt')]  # книги без папки темы
    for g in sorted(os.listdir(a.root)):
        d = os.path.join(a.root, g)
        if os.path.isdir(d):
            files += [os.path.join(d, f) for f in sorted(os.listdir(d)) if f.endswith('.txt')]
    res = {}
    with Pool(a.jobs, initializer=_init, initargs=(GDIR, MAXC)) as pool:
        for i, (p, r) in enumerate(pool.imap_unordered(one, files), 1):
            res[p] = r
            print(f'  {i}/{len(files)} {os.path.basename(p)}: {", ".join(r["suggest"]) or "только base"}', file=sys.stderr, flush=True)
    doms = sorted({d for r in res.values() for d in r['domains']})
    with open(a.out, 'w', encoding='utf-8', newline='') as f:
        w = csv.writer(f)
        w.writerow(['group', 'book', 'words', 'suggest'] + [f'{d}_multi' for d in doms] + [f'{d}_share' for d in doms])
        for p in files:
            r = res[p]
            w.writerow([os.path.basename(os.path.dirname(p)), os.path.basename(p), r['words'], ' '.join(r['suggest'])]
                       + [r['domains'][d]['multi'] for d in doms] + [r['domains'][d]['share'] for d in doms])
    print('\nТема (книг): сколько книг получили область', file=sys.stderr)
    by = collections.defaultdict(list)
    for p in files:
        by[os.path.basename(os.path.dirname(p))].append(res[p])
    for g, rs in by.items():
        c = collections.Counter(d for r in rs for d in r['suggest'])
        none = sum(1 for r in rs if not r['suggest'])
        print(f'  {g} ({len(rs)}): ' + ', '.join(f'{d} {n}' for d, n in c.most_common()) + (f', ничего {none}' if none else ''),
              file=sys.stderr)
    print(f'Файл: {a.out}', file=sys.stderr)


def _init(g, m):
    global GDIR, MAXC
    GDIR, MAXC = g, m


if __name__ == '__main__':
    main()
