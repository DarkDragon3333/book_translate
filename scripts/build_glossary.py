#!/usr/bin/env python3
"""Собирает глоссарий для конкретной книги из файлов config/glossary/*.csv.

Формат исходных файлов — как у pdf2zh: CSV с заголовком source,target,tgt_lng (UTF-8).
  base.csv         — общая IT-лексика, подключается всегда
  programming.csv, ml.csv, math.csv … — области, выбираются при запуске
  keep.csv         — непереводимые названия (SVD → SVD); только для bbm,
                     в pdf2zh пары-тождества путали модель
  source/<книга>.glossary.csv — необязательный словарь конкретной книги
При совпадении терминов побеждает более поздний файл (область > base, книга > область).

  python build_glossary.py --format bbm|pdf2zh --dir config/glossary --domains ml,math [--extra book.csv] [--out file]
"""
import argparse, csv, io, os, sys


def read(path):
    with open(path, encoding='utf-8-sig', newline='') as f:
        for row in csv.DictReader(f):
            s, t = (row.get('source') or '').strip(), (row.get('target') or '').strip()
            if s and t and not s.startswith('#'):
                yield s, t


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--format', choices=['bbm', 'pdf2zh'], required=True)
    ap.add_argument('--dir', default='/config/glossary')
    ap.add_argument('--domains', default='base')
    ap.add_argument('--extra')
    ap.add_argument('--out')
    a = ap.parse_args()

    domains = ['base'] + [d.strip() for d in a.domains.split(',') if d.strip() and d.strip() != 'base']
    files = [os.path.join(a.dir, d + '.csv') for d in domains]
    if a.format == 'bbm':
        files.insert(0, os.path.join(a.dir, 'keep.csv'))
    missing = [f for f in files if not os.path.isfile(f)]
    if missing:
        known = sorted(f[:-4] for f in os.listdir(a.dir) if f.endswith('.csv') and f != 'keep.csv')
        sys.exit('Нет файла глоссария: %s. Доступные области: %s' % (', '.join(missing), ', '.join(known)))
    if a.extra and os.path.isfile(a.extra):
        files.append(a.extra)

    terms, conflicts = {}, []
    for f in files:
        for s, t in read(f):
            key = s.lower()
            if key in terms and terms[key][1] != t:
                conflicts.append('%s: «%s» → «%s» (%s)' % (s, terms[key][1], t, os.path.basename(f)))
            terms[key] = (s, t)

    out = io.StringIO()
    if a.format == 'bbm':
        for s, t in terms.values():
            out.write('%s → %s\n' % (s, t))
    else:
        w = csv.writer(out, lineterminator='\n')
        w.writerow(['source', 'target', 'tgt_lng'])
        for s, t in terms.values():
            if s != t:
                w.writerow([s, t, ''])
    text = out.getvalue()
    if a.out:
        with open(a.out, 'w', encoding='utf-8') as f:
            f.write(text)
    else:
        sys.stdout.write(text)
    extra = ' + ' + os.path.basename(a.extra) if a.extra and os.path.isfile(a.extra) else ''
    print('    Терминов: %d (%s%s)' % (len(terms), ', '.join(domains), extra), file=sys.stderr)
    for c in conflicts:
        print('    Переопределён: ' + c, file=sys.stderr)


if __name__ == '__main__':
    main()
