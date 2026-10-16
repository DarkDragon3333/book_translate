#!/usr/bin/env python3
"""Проверка сервиса remedy: его оценки против оценок, по которым выбирался детектор (remedy_score.py, HF-токенизатор).

  python remedy_check.py <журнал 4B> <журнал 12B> <qe_ReMedy-9B-Q5_K_M_<глава>.jsonl> [--n 135]
Пары — как в experiments/qe/remedy_local/remedy_score.py (абзацы журнала 12B, найденные в журнале 4B; номер i — строка
журнала 12B); сравнивается оценка 4B (s4). Проверяются два варианта числа <bos> перед шаблоном (1 и 0):
совпадение (разница < 0,01, Spearman ≈ 1) значит, что токенизация сервиса та же, что у HF.
Перед оценкой выгружает модели Ollama, после — ReMedy.
"""
import argparse, json, os, re, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from hybrid_pick import http, unload_ollama

KEY = re.compile(r"⟦[^⟧]*⟧|[^\w]", re.U)
key = lambda s: KEY.sub("", s).lower()


def rank(v):
    o = sorted(range(len(v)), key=lambda i: v[i])
    r = [0.0] * len(v)
    i = 0
    while i < len(o):
        j = i
        while j + 1 < len(o) and v[o[j + 1]] == v[o[i]]:
            j += 1
        for k in range(i, j + 1):
            r[o[k]] = (i + j) / 2
        i = j + 1
    return r


def spearman(a, b):
    ra, rb = rank(a), rank(b)
    n = len(a)
    ma, mb = sum(ra) / n, sum(rb) / n
    cov = sum((x - ma) * (y - mb) for x, y in zip(ra, rb))
    va = sum((x - ma) ** 2 for x in ra) ** .5
    vb = sum((y - mb) ** 2 for y in rb) ** .5
    return cov / (va * vb)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('log4')
    ap.add_argument('log12')
    ap.add_argument('qe')
    ap.add_argument('--n', type=int, default=135)
    ap.add_argument('--remedy', default=os.environ.get('REMEDY_URL', 'http://remedy:8091'))
    ap.add_argument('--ollama', default=os.environ.get('OLLAMA_URL', 'http://ollama:11434'))
    a = ap.parse_args()
    load = lambda p: [json.loads(l) for l in open(p, encoding='utf-8') if l.strip()]
    r4, r12 = load(a.log4), load(a.log12)
    idx = {}
    for r in r4:
        idx.setdefault(key(r['src']), r)
    rec4 = {i: idx[key(r['src'])] for i, r in enumerate(r12) if len(key(r['src'])) >= 3 and key(r['src']) in idx}
    ref = {q['i']: q['s4'] for q in load(a.qe)}
    ids = [i for i in sorted(ref) if i in rec4][:a.n]
    print('Пар для сравнения: %d' % len(ids), flush=True)
    st = http(a.remedy + '/status', timeout=10)
    print('Сервис remedy: модель %s, файлы %s' % (st['model'], 'есть' if st['available'] else 'НЕТ'), flush=True)
    if not st['available']:
        sys.exit(1)
    unload_ollama(a.ollama)
    try:
        for eb in (1, 0):
            got = []
            for k in range(0, len(ids), 16):
                part = [rec4[i] for i in ids[k:k + 16]]
                got += http(a.remedy + '/score', {'pairs': [{'src': r['src'], 'mt': r['out']} for r in part],
                                                  'extra_bos': eb}, timeout=1800)['scores']
            want = [ref[i] for i in ids]
            ok = [(g, w) for g, w in zip(got, want) if g is not None]
            diff = max(abs(g - w) for g, w in ok)
            print('extra_bos=%d: Spearman %.4f, наибольшая разница %.4f, средняя %.4f  →  %s' % (
                eb, spearman([g for g, _ in ok], [w for _, w in ok]), diff,
                sum(abs(g - w) for g, w in ok) / len(ok), 'СОВПАДАЕТ' if diff < 0.05 else 'не совпадает'), flush=True)
            if diff < 0.05:
                break
    finally:
        http(a.remedy + '/unload', {}, timeout=60)


if __name__ == '__main__':
    main()
