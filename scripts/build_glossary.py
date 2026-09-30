#!/usr/bin/env python3
"""Собирает глоссарий для конкретной книги из файлов config/glossary/*.csv.

Формат исходных файлов — как у pdf2zh: CSV с заголовком source,target,tgt_lng (UTF-8).
  base.csv         — общая IT-лексика, подключается всегда
  programming.csv, ml.csv, math.csv … — области, выбираются при запуске
  keep.csv         — непереводимые названия (SVD → SVD); только для bbm,
                     в pdf2zh пары-тождества путали модель
  source/<книга>.glossary.csv — необязательный словарь конкретной книги
При совпадении терминов побеждает более поздний файл (область > base, книга > область).
Для bbm к каждому термину добавляется английское множественное число с тем же переводом
(bbm ищет термин точной формой целым словом: «neural network» не срабатывает на «neural networks»;
перевод в именительном падеже единственного числа модель склоняет сама).

  python build_glossary.py --format bbm|pdf2zh --dir config/glossary --domains ml,math [--extra book.csv] [--out file]
"""
import argparse, csv, io, os, sys


def read(path):
    with open(path, encoding='utf-8-sig', newline='') as f:
        for row in csv.DictReader(f):
            s, t = (row.get('source') or '').strip(), (row.get('target') or '').strip()
            if s and t and not s.startswith('#'):
                if '#' in s or '#' in t:     # bbm считает «#» началом комментария: строка с ним роняет перевод
                    print('    Пропущен термин с «#»: %s' % s, file=sys.stderr)
                    continue
                yield s, t


IRREGULAR = {'matrix': ['matrices'], 'index': ['indexes', 'indices'], 'vertex': ['vertices'], 'hypothesis': ['hypotheses'],
             'analysis': ['analyses'], 'axis': ['axes'], 'criterion': ['criteria'], 'leaf': ['leaves'], 'child': ['children'],
             'basis': ['bases'], 'datum': ['data'], 'appendix': ['appendices'], 'phenomenon': ['phenomena']}
UNCOUNTABLE = {'data', 'software', 'hardware', 'feedback', 'information', 'knowledge', 'research', 'evidence',
               'code', 'middleware', 'malware', 'ransomware', 'overhead', 'metadata', 'bandwidth', 'throughput'}


def plurals(term):
    """Английские формы множественного числа последнего слова термина: cache miss → cache misses."""
    head, sep, last = term.rpartition(' ')
    if not last.isalpha() or not last.islower() or last in UNCOUNTABLE or len(last) < 3:
        return []
    if last in IRREGULAR:
        forms = IRREGULAR[last]
    elif last.endswith(('s', 'x', 'z', 'ch', 'sh')):
        forms = [last + 'es']
    elif last.endswith('y') and last[-2] not in 'aeiou':
        forms = [last[:-1] + 'ies']
    else:
        forms = [last + 's']
    return [head + sep + f for f in forms]


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

    n_terms = len(terms)
    if a.format == 'bbm':
        keep = {s.lower() for s, _ in read(os.path.join(a.dir, 'keep.csv'))}
        for key, (s, t) in list(terms.items()):
            if key in keep:
                continue
            for p in plurals(s):
                terms.setdefault(p.lower(), (p, t))

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
    forms = ' + %d форм мн. ч.' % (len(terms) - n_terms) if len(terms) > n_terms else ''
    print('    Терминов: %d%s (%s%s)' % (n_terms, forms, ', '.join(domains), extra), file=sys.stderr)
    for c in conflicts:
        print('    Переопределён: ' + c, file=sys.stderr)


if __name__ == '__main__':
    main()
