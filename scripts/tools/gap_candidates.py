#!/usr/bin/env python3
"""Кандидаты в глоссарий по выдуманным словам перевода (14.10).

Закономерность (13.10, 41 ошибка смысла на 800 абзацах + гл. 10): модель выдумывает слово или берёт не тот термин
почти всегда там, где для английского слова не было подсказки глоссария (batch → «мини-бэч», nonsequential →
«несеквенциальных», softmax → «софтмакс»). ru_check находит выдуманные русские слова; этот скрипт для каждого
такого абзаца показывает английские слова и сочетания, у которых не было подсказки и которых нет в глоссарии,
и сводит их в список кандидатов. Решение — за человеком: добавить в глоссарий, keep.csv или senses.csv.

  python gap_candidates.py /books/<работа>/.rosetta_log.jsonl --check /books/<работа>/.ru_check.json
         [--glossary /config/glossary] [--senses /config/senses.csv] [--max-zipf 4.6] [--out кандидаты.txt]
"""
import argparse, collections, json, os, re, sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import term_gaps as tg   # noqa: E402  (тот же разбор слов, стоп-слова и частоты обычного английского)

MARK = re.compile(r'⟦[^⟧]*⟧')


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('log')
    ap.add_argument('--check', required=True, help='.ru_check.json (пишет epub.sh)')
    ap.add_argument('--glossary', default='/config/glossary')
    ap.add_argument('--senses', default='/config/senses.csv')
    ap.add_argument('--zipf', default=os.path.join(os.path.dirname(os.path.abspath(__file__)), 'en_zipf.tsv.gz'))
    ap.add_argument('--max-zipf', type=float, default=4.1,
                    help='слово чаще этого в обычном английском (the=7.7, table≈5.0, batch≈4.0) — не кандидат')
    ap.add_argument('--out')
    a = ap.parse_args()

    zipf = tg.load_zipf(a.zipf)
    tg.ZIPF.update(zipf)
    have = tg.load_glossary(a.glossary, a.senses)
    log = [json.loads(l) for l in open(a.log, encoding='utf-8') if l.strip()]
    check = json.load(open(a.check, encoding='utf-8'))

    lines, total, where = [], collections.Counter(), collections.defaultdict(set)
    for item in check:
        bad = [w for w in item['words']]
        hits = [i for i, r in enumerate(log) if any(w.lower() in r.get('out', '').lower() for w in bad)]
        if not hits:
            lines.append('— %s: абзац в журнале не найден' % ', '.join(bad))
            continue
        for i in hits:
            r = log[i]
            hinted = {' '.join(tg.lemma(tg.norm(x)) for x in t[0].split()) for t in r.get('terms', [])}
            covered = lambda k: k in have or k in hinted
            text = MARK.sub(' | ', r['src'])
            # предложение оригинала, где стоит выдуманное слово: по номеру предложения в переводе
            # (если число предложений не совпало — по доле позиции, ±1 предложение)
            es = [x for x in tg.SENT.split(text) if x.strip()]
            rs = [x for x in re.split(r'(?<=[.!?])\s+(?=[А-ЯЁA-Z⟦«"(])', r['out']) if x.strip()]
            k = next((j for j, x in enumerate(rs) if any(w.lower() in x.lower() for w in bad)), 0)
            if len(es) == len(rs):
                pick = [k]
            else:
                c = round(k * (len(es) - 1) / max(1, len(rs) - 1))
                pick = [j for j in (c - 1, c, c + 1) if 0 <= j < len(es)]
            cands = collections.OrderedDict()
            for j in pick:
                for chunk in re.split(r'[,;:()\[\]{}"“”«»—–|]|\s-\s', es[j]):
                    toks = tg.TOKEN.findall(chunk)
                    words = [tg.lemma(tg.norm(t)) for t in toks]
                    for n, (t, w) in enumerate(zip(toks, words)):   # слова, редкие в обычном английском
                        if w in tg.STOP or len(w) < 3 or covered(w):
                            continue
                        if t[0].isupper() and t[1:].islower() and n > 0:
                            continue                                 # имя собственное
                        if zipf.get(w, 0.0) <= a.max_zipf:
                            cands[t if t.isupper() else w] = zipf.get(w, 0.0)
                    for n in (2, 3):                     # сочетания с редким словом
                        for q in range(len(words) - n + 1):
                            g = words[q:q + n]
                            if g[0] in tg.STOP or g[-1] in tg.STOP or covered(' '.join(g)):
                                continue
                            if n == 3 and g[1] in tg.STOP and g[1] != 'of':
                                continue
                            z = min(zipf.get(x, 0.0) for x in g)
                            if z <= a.max_zipf - 0.6:
                                cands[' '.join(g)] = z
            best = sorted(cands, key=lambda c: cands[c])[:8]
            lines.append('№%d  %s' % (i, ', '.join(bad)))
            lines.append('    EN: ' + re.sub(r'\s+', ' ', ' '.join(es[j] for j in pick))[:300])
            lines.append('    без подсказки (сначала редкие): ' + ('; '.join(best) if best else '—'))
            for c in best:
                total[c] += 1
                where[c].add(i)

    out = ['Кандидаты в глоссарий: абзацев с выдуманными словами %d' % len(check), '']
    out += lines
    out += ['', 'Сводка (сколько абзацев с выдуманными словами содержит слово или сочетание):']
    for c, n in total.most_common(80):
        z = zipf.get(c)
        out.append('  %-35s %d%s' % (c, len(where[c]), '' if z is None else '   (частота в английском %.1f)' % z))
    text = '\n'.join(out)
    if a.out:
        with open(a.out, 'w', encoding='utf-8') as f:
            f.write(text + '\n')
        print('Записано: %s' % a.out)
    else:
        print(text)


if __name__ == '__main__':
    main()
