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
С --book (EPUB, HTML или TXT книги) добавляются сочетания терминов с общим последним словом, которые
встречаются в тексте: «liveness and readiness probes» при терминах liveness probe и readiness probe.
Иначе подходит только одна из записей, и модель ставит её перевод не к тому слову (Spring in Action, 10.10.2026).

  python build_glossary.py --format bbm|pdf2zh --dir config/glossary --domains ml,math [--extra book.csv] [--book book.epub] [--out file]
"""
import argparse, csv, html, io, os, re, sys, zipfile


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
    if last.endswith('s') and not last.endswith(('ss', 'us', 'is', 'as')):   # уже мн. ч. (features, time series)
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


def book_text(path):
    """Текст книги без разметки и блоков кода."""
    if path.lower().endswith('.epub'):
        z = zipfile.ZipFile(path)
        parts = [z.read(n).decode('utf-8', 'ignore') for n in sorted(z.namelist()) if n.lower().endswith(('.xhtml', '.html', '.htm'))]
    else:
        with open(path, encoding='utf-8', errors='ignore') as f:
            parts = [f.read()]
    text = ' '.join(re.sub(r'<pre\b.*?</pre>', ' ', p, flags=re.S | re.I) for p in parts)
    return html.unescape(re.sub(r'<[^>]+>', ' ', text))


COORD = re.compile(r"\b((?:[A-Za-z][\w-]*, ){0,3}[A-Za-z][\w-]*,? (?:and|or) [A-Za-z][\w-]*) ([A-Za-z][\w-]*)\b")
SINGULAR = {f: k for k, v in IRREGULAR.items() for f in v}


def singulars(w):
    out = [w, SINGULAR.get(w, '')]
    if w.endswith('ies'):
        out.append(w[:-3] + 'y')
    if w.endswith('es'):
        out.append(w[:-2])
    if w.endswith('s'):
        out.append(w[:-1])
    return [x for x in out if x]


def join_ru(items, conj):
    return items[0] if len(items) == 1 else ', '.join(items[:-1]) + ' ' + conj + ' ' + items[-1]


def combine_ru(targets, conj):
    """проба живучести + проба готовности → проба живучести и готовности; модульный тест + интеграционный тест →
    модульный и интеграционный тест; без общей части — переводы целиком через «и»."""
    ws = [re.sub(r'\s*\([^)]*\)', '', t).split() for t in targets]
    n = min(len(w) for w in ws)
    suf = 0
    while suf < n - 0 and len({tuple(w[len(w) - suf - 1:]) for w in ws}) == 1 and all(len(w) > suf + 1 for w in ws):
        suf += 1
    pre = 0
    while pre < n and len({tuple(w[:pre + 1]) for w in ws}) == 1 and all(len(w) > pre + 1 for w in ws):
        pre += 1
    if suf and all(len(w) - suf == 1 for w in ws):      # различие — одно слово, иначе склейка выходит кривой
        return join_ru([' '.join(w[:len(w) - suf]) for w in ws], conj) + ' ' + ' '.join(ws[0][len(ws[0]) - suf:])
    if pre and all(len(w) - pre == 1 for w in ws):
        return ' '.join(ws[0][:pre]) + ' ' + join_ru([' '.join(w[pre:]) for w in ws], conj)
    return join_ru([' '.join(w) for w in ws], conj)


def coordinations(text, terms, skip):
    """Сочетания «A and B Xs» / «A, B or C X» из текста книги, где каждое «A X» есть в глоссарии."""
    found = {}
    for m in COORD.finditer(text):
        phrase = m.group(0).lower()
        if phrase in terms or phrase in found:
            continue
        mods = [x for x in re.split(r',? (?:and|or) |, ', m.group(1).lower()) if x]
        conj = 'или' if re.search(r'\bor\b', m.group(1)) else 'и'
        for head in singulars(m.group(2).lower()):
            keys = [mod + ' ' + head for mod in mods]
            if len(mods) > 1 and all(k in terms and k not in skip and terms[k][0] != terms[k][1] for k in keys):
                found[phrase] = (phrase, combine_ru([terms[k][1] for k in keys], conj))
                break
    return found


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--format', choices=['bbm', 'pdf2zh'], required=True)
    ap.add_argument('--dir', default='/config/glossary')
    ap.add_argument('--domains', default='base')
    ap.add_argument('--extra')
    ap.add_argument('--book')
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

    n_coord = 0
    if a.book and os.path.isfile(a.book):
        try:
            keep = {s.lower() for s, _ in read(os.path.join(a.dir, 'keep.csv'))} if os.path.isfile(os.path.join(a.dir, 'keep.csv')) else set()
            for key, v in coordinations(book_text(a.book), terms, keep).items():
                if key not in terms:
                    terms[key] = v
                    n_coord += 1
        except Exception as e:                 # сочетания — улучшение, перевод без них не должен падать
            print('    Сочетания терминов не собраны: %s' % e, file=sys.stderr)

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
    forms = ' + %d форм мн. ч.' % (len(terms) - n_terms - n_coord) if len(terms) - n_coord > n_terms else ''
    forms += ' + %d сочетаний «A и B» из книги' % n_coord if n_coord else ''
    print('    Терминов: %d%s (%s%s)' % (n_terms, forms, ', '.join(domains), extra), file=sys.stderr)
    for c in conflicts:
        print('    Переопределён: ' + c, file=sys.stderr)


if __name__ == '__main__':
    main()
