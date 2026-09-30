#!/usr/bin/env python3
"""Оценка перевода набора с эталоном: chrF (как sacrebleu: символьные n-граммы до 6, β=2, без пробелов).

  python eval_chrf.py eval_prog_ref.jsonl eval_prog_ru.epub [--by file|source] [--json out.json]

Абзацы перевода ищутся по id (<p id="git000">); без id — по порядку. Печатает chrF всего набора и по источникам
(git — Pro Git, mdn — MDN), плюс худшие абзацы. Два перевода сравниваются запуском на каждом.
"""
import argparse, json, re, zipfile
from collections import Counter
from html.parser import HTMLParser

ORDER, BETA = 6, 2


def ngrams(s, n):
    s = re.sub(r'\s+', '', s)
    return Counter(s[i:i + n] for i in range(len(s) - n + 1))


def stats(hyp, ref):
    out = []
    for n in range(1, ORDER + 1):
        h, r = ngrams(hyp, n), ngrams(ref, n)
        out.append((sum((h & r).values()), sum(h.values()), sum(r.values())))
    return out


def chrf(stat_list):
    """Корпусный chrF по сумме статистик (как sacrebleu CHRF.corpus_score)."""
    tot = [[0, 0, 0] for _ in range(ORDER)]
    for st in stat_list:
        for i, (m, h, r) in enumerate(st):
            tot[i][0] += m; tot[i][1] += h; tot[i][2] += r
    ps, rs = [], []
    for m, h, r in tot:
        if h and r:
            ps.append(m / h); rs.append(m / r)
    if not ps:
        return 0.0
    p, r = sum(ps) / len(ps), sum(rs) / len(rs)
    if p + r == 0:
        return 0.0
    b2 = BETA ** 2
    return 100 * (1 + b2) * p * r / (b2 * p + r)


class _P(HTMLParser):
    def __init__(self):
        super().__init__()
        self.cur, self.id, self.out, self.depth = [], None, [], 0

    def handle_starttag(self, t, a):
        if t == 'p':
            self.depth += 1
            self.id = dict(a).get('id')
            self.cur = []

    def handle_endtag(self, t):
        if t == 'p' and self.depth:
            self.depth -= 1
            self.out.append((self.id, re.sub(r'\s+', ' ', ''.join(self.cur)).strip()))

    def handle_data(self, d):
        if self.depth:
            self.cur.append(d)


def epub_paras(path):
    z = zipfile.ZipFile(path)
    res = []
    for n in sorted(z.namelist()):
        if n.endswith(('.xhtml', '.html')) and 'nav' not in n.rsplit('/', 1)[-1]:
            p = _P()
            p.feed(z.read(n).decode('utf-8', 'replace'))
            res += p.out
    return res


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('ref')
    ap.add_argument('hyp')
    ap.add_argument('--json')
    a = ap.parse_args()
    refs = [json.loads(l) for l in open(a.ref, encoding='utf-8')]
    paras = epub_paras(a.hyp)
    by_id = {i: t for i, t in paras if i}
    hyp = [by_id.get(r['id']) for r in refs]
    if sum(h is None for h in hyp) > len(refs) // 2:           # id потерялись — по порядку
        hyp = [t for _, t in paras][:len(refs)]
    rows = []
    for r, h in zip(refs, hyp):
        h = re.sub(r'^\[![^\]]*\]\s*', '', h or '')        # метка [!NOTE] из старой версии набора
        st = stats(h, r['ref'])
        rows.append({'id': r['id'], 'st': st, 'chrf': chrf([st]), 'src': r['src'], 'ref': r['ref'], 'hyp': h})
    print('chrF всего набора: %.1f  (абзацев %d, без перевода %d)' % (
        chrf([x['st'] for x in rows]), len(rows), sum(not x['hyp'] for x in rows)))
    for pref, name in (('git', 'Pro Git'), ('mdn', 'MDN')):
        part = [x['st'] for x in rows if x['id'].startswith(pref)]
        if part:
            print('  %-8s %.1f  (%d)' % (name, chrf(part), len(part)))
    print('\nХудшие абзацы:')
    for x in sorted(rows, key=lambda x: x['chrf'])[:5]:
        print('  %s chrF %.0f: %s' % (x['id'], x['chrf'], x['src'][:90]))
    if a.json:
        json.dump([{k: v for k, v in x.items() if k != 'st'} for x in rows], open(a.json, 'w', encoding='utf-8'),
                  ensure_ascii=False, indent=1)


if __name__ == '__main__':
    main()
