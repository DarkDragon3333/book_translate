#!/usr/bin/env python3
"""Проверка правки глоссария и senses.csv на книге: что изменится в подсказках модели.

  python senses_diff.py книга.epub --domains ml,math --old-dir /books/.gloss_old [--examples 6]
  Вместо EPUB можно текст корпуса (books/.corpus*/…/*.txt, абзац = строка; главы не различаются).

--old-dir — копия config до правки (senses.csv и glossary/), новая берётся из /config.
Печатает: 1) новые термины глоссария — сколько раз встречаются в книге и пару примеров (видно слишком широкие);
2) решения senses по каждому спорному слову до/после (свой / другой / не подсказывать) и примеры изменившихся абзацев.
Ничего не меняет и не переводит."""
import argparse, csv, os, re, sys
from collections import Counter
sys.path.insert(0, '/scripts')
import senses as sn  # noqa: E402
from build_glossary import plurals  # noqa: E402


def terms(gdir, domains):
    out = {}
    for f in sorted(os.listdir(gdir)):
        if not f.endswith('.csv') or (f[:-4] not in domains and f[:-4] not in ('base', 'keep')):
            continue
        with open(os.path.join(gdir, f), encoding='utf-8-sig') as h:
            for r in csv.DictReader(line for line in h if not line.startswith('#')):
                s = (r.get('source') or '').strip()
                if s:
                    out[s] = (r.get('target') or '').strip()
    return out


def snip(text, m, w=70):
    return '…' + text[max(0, m.start() - w):m.end() + w].replace('\n', ' ') + '…'


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('epub')
    ap.add_argument('--domains', required=True)
    ap.add_argument('--old-dir', required=True)
    ap.add_argument('--new-dir', default='/config')
    ap.add_argument('--examples', type=int, default=6)
    ap.add_argument('--show-own', action='store_true', help='показывать и примеры «свой перевод» у изменённых слов')
    a = ap.parse_args()
    doms = {d for d in a.domains.split(',') if d and d != 'base'}
    if a.epub.lower().endswith('.txt'):
        with open(a.epub, encoding='utf-8') as f:
            paras = [{'text': ln.strip(), 'ch': 0, 'sec': 0} for ln in f if len(ln.strip()) > 1]
    else:
        paras = sn.epub_paragraphs(a.epub)
    print('Книга: %s, абзацев %d, области %s' % (os.path.basename(a.epub), len(paras), ','.join(sorted(doms))))

    old_t, new_t = terms(os.path.join(a.old_dir, 'glossary'), doms), terms(os.path.join(a.new_dir, 'glossary'), doms)
    added = [t for t in new_t if t not in old_t]
    print('\n1. Новые термины глоссария в этой книге (областей %s):' % ','.join(sorted(doms)))
    for t in added:
        forms = [t] + plurals(t)
        flags = 0 if t != t.lower() else re.I
        rx = re.compile(r'(?<!\w)(?:%s)(?!\w)' % '|'.join(map(re.escape, forms)), flags)
        hits = [(p['text'], m) for p in paras for m in [rx.search(p['text'])] if m]
        print('  %-36s → %-40s абзацев %d' % (t, new_t[t], len(hits)))
        for text, m in hits[:2]:
            print('      ' + snip(text, m, 50))

    old_r = sn.load_senses(os.path.join(a.old_dir, 'senses.csv'))
    new_r = sn.load_senses(os.path.join(a.new_dir, 'senses.csv'))
    d_old = sn.decide_paragraphs(paras, old_r, sn.load_multi(os.path.join(a.old_dir, 'glossary')), doms)
    d_new = sn.decide_paragraphs(paras, new_r, sn.load_multi(os.path.join(a.new_dir, 'glossary')), doms)
    print('\n2. Смысл по контексту (senses): решения до → после')
    for w in sorted(set(new_r) | set(old_r)):
        c_old = Counter(d_old[i][w][0] for i in d_old if w in d_old[i])
        c_new = Counter(d_new[i][w][0] for i in d_new if w in d_new[i])
        ch = [i for i in set(d_old) | set(d_new)
              if (d_old.get(i, {}).get(w) or (None,))[0] != (d_new.get(i, {}).get(w) or (None,))[0]]
        if not ch and not c_new:
            continue
        print('  %-12s до: свой %d, другой %d, нет %d | после: свой %d, другой %d, нет %d | изменилось абзацев %d'
              % (w, c_old['own'], c_old['alt'], c_old['none'], c_new['own'], c_new['alt'], c_new['none'], len(ch)))
        rx = new_r.get(w, old_r.get(w))['forms']
        show = lambda i, tag: print('      [%s] %s' % (tag, snip(paras[i]['text'], rx.search(paras[i]['text']), 60)
                                                     if rx.search(paras[i]['text']) else paras[i]['text'][:120]))
        moved = [i for i in sorted(ch) if (d_old.get(i, {}).get(w) or ('-',))[0] != '-' or w in old_r]
        for i in moved[:a.examples]:                         # смена решения у слова, которое было и раньше
            o, n = d_old.get(i, {}).get(w), d_new.get(i, {}).get(w)
            show(i, '%s → %s' % (o[0] if o else '-', n[0] if n else '-'))
        for kind, tag in ((('own', 'свой: '),) if a.show_own else ()) + (('alt', 'другой: '), ('none', 'не подсказывать')) if ch else ():  # у изменённых слов — редкие решения
            idx = [i for i in sorted(d_new) if w in d_new[i] and d_new[i][w][0] == kind]
            for i in idx[:a.examples]:
                show(i, tag + (d_new[i][w][1] or ''))
            if len(idx) > a.examples:
                print('      … ещё %d' % (len(idx) - a.examples))

if __name__ == '__main__':
    main()
