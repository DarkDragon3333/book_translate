#!/usr/bin/env python3
"""Постраничная проверка перевода pdf2zh против оригинала.

На каждой странице считает:
  missing/extra — идентификаторы кода (слова шрифтом кода), пропавшие/лишние в переводе;
  cyr_code      — русские слова, набранные шрифтом кода (в коде их быть не может: «и» в конце строк);
  foreign       — «чужие» строки: одинаковый русский текст в теле 4+ разных страниц
                  (так выглядит подмена кода фрагментом оглавления/рекламы; колонтитулы не считаются);
  untranslated  — страница с прозой почти без кириллицы.
score — чем меньше, тем лучше; по нему repair_pdf.py выбирает лучшую версию страницы.

  python qa_pdf.py orig.pdf tran.pdf --code-font '.*(Courier).*' [--json out.json]
"""
import argparse, json, re
from collections import Counter, defaultdict
import pdfplumber

ID = re.compile(r'[A-Za-z_$][A-Za-z0-9_$]*')
CYR = re.compile(r'[А-Яа-яЁё]')
CJK = re.compile(r'[一-鿿぀-ヿ]')
MARGIN = 0.08          # верх/низ страницы — колонтитулы


def page_info(page, code_re):
    words = page.extract_words(extra_attrs=['fontname'])
    code, prose, cyr_code, chars, tops = Counter(), [], 0, 0, set()
    for w in words:
        t = w['text'].replace('(cid:3)', ' ')
        if code_re.search(w['fontname']):
            code.update(ID.findall(t))
            chars += len(t.replace(' ', ''))
            tops.add(round(w['top']))
            if CYR.search(t):
                cyr_code += 1
        else:
            prose.append(t)
    h = float(page.height)
    lines = []
    for ln in page.extract_text_lines():
        if MARGIN * h < ln['top'] < (1 - MARGIN) * h:
            t = re.sub(r'\s+', ' ', ln['text'].replace('(cid:3)', ' ')).strip()
            if len(t) > 12 and CYR.search(t):
                lines.append((t, round(ln['top'])))
    return {'code': code, 'prose': ' '.join(prose), 'cyr_code': cyr_code, 'lines': lines,
            'chars': chars, 'tops': tops}


def collect(path, code_re, pages=None):
    out = {}
    with pdfplumber.open(path) as pdf:
        for i in (pages or range(len(pdf.pages))):
            p = pdf.pages[i]
            out[i] = page_info(p, code_re)
            p.close()
    return out


def score_page(o, t, foreign_lines):
    total = sum(o['code'].values())
    missing = sum((o['code'] - t['code']).values())
    extra = sum((t['code'] - o['code']).values())
    loss_ids = missing / total if total >= 20 else 0.0
    loss_chars = max(0.0, 1 - t['chars'] / o['chars']) if o['chars'] >= 40 else 0.0
    loss_lines = max(0.0, 1 - len(t['tops']) / len(o['tops'])) if len(o['tops']) >= 3 else 0.0
    loss = max(loss_ids, loss_chars, loss_lines / 2)
    # подмена: повторяющаяся «чужая» строка стоит там, где в оригинале был код
    foreign = sum(1 for l, top in t['lines'] if l in foreign_lines and any(abs(top - c) <= 3 for c in o['tops']))
    cyr = len(CYR.findall(t['prose'])); lat = len(re.findall(r'[A-Za-z]', t['prose']))
    untranslated = len(o['prose']) > 800 and cyr / (cyr + lat + 1) < 0.35
    cjk = bool(CJK.search(t['prose']))
    bad = loss > 0.1 or t['cyr_code'] >= 3 or foreign > 0 or untranslated or cjk
    score = missing + extra + abs(o['chars'] - t['chars']) // 10 + 10 * t['cyr_code'] + 30 * foreign \
        + (500 if untranslated else 0) + (100 if cjk else 0)
    lost_chars = max(0, o['chars'] - t['chars'])
    # вставлять английский оригинал стоит, только если кода пропало заметно или есть подмена
    severe = foreign > 0 or (loss > 0.1 and max(lost_chars, missing * 4) >= 40)
    return {'total': total, 'missing': missing, 'extra': extra, 'loss': round(loss, 3), 'cyr_code': t['cyr_code'],
            'severe': severe,
            'foreign': foreign, 'untranslated': untranslated, 'cjk': cjk, 'bad': bad, 'score': score}


def find_foreign(tran):
    pages = defaultdict(set)
    for i, info in tran.items():
        for l in {l for l, _ in info['lines']}:
            pages[l].add(i)
    # обычные фразы перед листингами («следующим образом:») и одиночные слова — не подмена
    return {l for l, p in pages.items() if len(p) >= 4 and len(l.split()) >= 2 and not l.endswith(':')}


def check(orig_path, tran_path, code_font, orig=None):
    code_re = re.compile(code_font) if code_font else re.compile(r'$^')
    orig = orig or collect(orig_path, code_re)
    tran = collect(tran_path, code_re)
    foreign = find_foreign(tran)
    res = {i + 1: score_page(orig[i], tran[i], foreign) for i in tran if i in orig}
    return res, foreign, orig


def summary(res, foreign):
    bad = [p for p, r in res.items() if r['bad']]
    lines = ['Проверено страниц: %d, проблемных: %d' % (len(res), len(bad))]
    for p in bad[:60]:
        r = res[p]
        why = []
        if r['loss'] > 0.1: why.append('потеряно ~%d%% кода' % round(r['loss'] * 100))
        if r['cyr_code']: why.append('русский текст в коде: %d' % r['cyr_code'])
        if r['foreign']: why.append('чужие строки: %d' % r['foreign'])
        if r['untranslated']: why.append('не переведена')
        if r['cjk']: why.append('китайские символы')
        lines.append('  стр. %d: %s' % (p, '; '.join(why)))
    if foreign:
        lines.append('Строки-подмены (на 4+ страницах): ' + ' | '.join(sorted(foreign)[:5]))
    return '\n'.join(lines)


if __name__ == '__main__':
    ap = argparse.ArgumentParser()
    ap.add_argument('orig'); ap.add_argument('tran')
    ap.add_argument('--code-font', default='.*Courier.*')
    ap.add_argument('--json')
    a = ap.parse_args()
    res, foreign, _ = check(a.orig, a.tran, a.code_font)
    print(summary(res, foreign))
    if a.json:
        json.dump({'pages': res, 'foreign': sorted(foreign)}, open(a.json, 'w'), ensure_ascii=False, indent=0)
