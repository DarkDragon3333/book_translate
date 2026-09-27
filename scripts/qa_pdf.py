#!/usr/bin/env python3
"""Постраничная проверка перевода pdf2zh против оригинала.

На каждой странице считает:
  missing/extra — идентификаторы кода (слова шрифтом кода), пропавшие/лишние в переводе;
  cyr_code      — русские слова, набранные шрифтом кода (в коде их быть не может: «и» в конце строк);
  foreign       — «чужие» строки: одинаковый русский текст в теле 4+ разных страниц
                  (так выглядит подмена кода фрагментом оглавления/рекламы; колонтитулы не считаются);
  intrude       — русский текст обычным шрифтом прямо на строках кода оригинала (не на месте
                  прозы/сносок): фраза, повторяющаяся на 2+ страницах («Предисловие к первой редакции»),
                  заголовок раздела («4.4.7 …») или текст, приклеенный слева к строке листинга.
                  Код при этом часто цел, поэтому потеря кода такие страницы не ловит;
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
HEAD = re.compile(r'^(\d+(\.\d+)+\s|Глава\s|Пример\s|Листинг\s|Раздел\s|Рисунок\s)')
TRIVIAL = {'и', 'или', ',', '.'}


def group_lines(words, tol=2.5):
    """Слова → строки по вертикали; в строке слова слева направо."""
    out = []
    for w in sorted(words, key=lambda w: (round(w['top']), w['x0'])):
        if out and abs(out[-1][0]['top'] - w['top']) <= tol:
            out[-1].append(w)
        else:
            out.append([w])
    return [sorted(l, key=lambda w: w['x0']) for l in out]


def box(ws, pad=0.0):
    return (min(w['x0'] for w in ws) - pad, max(w['x1'] for w in ws) + pad,
            min(w['top'] for w in ws) - pad, max(w['bottom'] for w in ws) + pad)


def hits(a, b, m):
    return min(a[1], b[1]) - max(a[0], b[0]) > m and min(a[3], b[3]) - max(a[2], b[2]) > m


def norm(s):
    return re.sub(r'[^а-яё ]', '', s.lower()).strip()


def layout(words, is_code):
    """Для оригинала: строки кода (≥80% символов шрифтом кода; listing — в блоке из 3+ строк)
    и рамки остальных строк. Для перевода: русские фрагменты обычным шрифтом."""
    code, other, runs = [], [], []
    for l in group_lines(words):
        n_code = sum(len(w['text']) for w in l if is_code(w))
        if n_code >= 3 and n_code >= 0.8 * sum(len(w['text']) for w in l):
            code.append({'top': l[0]['top'], 'boxes': [box([w]) for w in l if is_code(w)]})
        else:
            other.append(box(l, 2))
        cur = []
        for w in l + [None]:
            cyr = w is not None and CYR.search(w['text']) and not is_code(w)
            if cyr and (not cur or w['x0'] - cur[-1]['x1'] < 12):   # большой разрыв — другой фрагмент
                cur.append(w)
                continue
            if cur:
                t = re.sub(r'\s+', ' ', ' '.join(x['text'] for x in cur).replace('(cid:3)', ' ')).strip()
                right = any(is_code(x) and x['x0'] >= cur[-1]['x1'] - 1 for x in l)
                if t.strip(' ,.') not in TRIVIAL:
                    runs.append({'text': t, 'boxes': [box([x]) for x in cur], 'right': right})
                cur = [w] if cyr else []
    code.sort(key=lambda c: c['top'])
    blocks = []                    # листинг — блок из 3+ строк кода с шагом не больше 16 pt
    for c in code:
        if blocks and c['top'] - blocks[-1][-1]['top'] <= 16:
            blocks[-1].append(c)
        else:
            blocks.append([c])
    for b in blocks:
        for c in b:
            c['listing'] = len(b) >= 3
    return code, other, runs


def code_runs(o, t):
    """Русские фрагменты перевода, лежащие на строках кода оригинала (не на прозе/сносках)."""
    out = []
    for r in t['runs']:
        if any(hits(b, ob, 0.5) for b in r['boxes'] for ob in o['other']):
            continue
        for c in o['code_lines']:
            if any(hits(b, cb, 1) for b in r['boxes'] for cb in c['boxes']):
                out.append((r, c['listing']))
                break
    return out


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
    code_lines, other, runs = layout(words, lambda w: bool(code_re.search(w['fontname'])))
    return {'code': code, 'prose': ' '.join(prose), 'cyr_code': cyr_code, 'lines': lines,
            'chars': chars, 'tops': tops, 'code_lines': code_lines, 'other': other, 'runs': runs}


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
    # текст на строках кода: повторяющаяся фраза, заголовок раздела или текст, приклеенный слева к листингу
    intrude = []
    phrases = [f[1:] for f in foreign_lines if f.startswith('~')]
    for r, listing in code_runs(o, t):
        n = norm(r['text'])
        if any(f in n for f in phrases) or HEAD.match(r['text']) or (listing and r['right']):
            intrude.append(r['text'])
    cyr = len(CYR.findall(t['prose'])); lat = len(re.findall(r'[A-Za-z]', t['prose']))
    untranslated = len(o['prose']) > 800 and cyr / (cyr + lat + 1) < 0.35
    cjk = bool(CJK.search(t['prose']))
    bad = loss > 0.1 or t['cyr_code'] >= 3 or foreign > 0 or bool(intrude) or untranslated or cjk
    score = missing + extra + abs(o['chars'] - t['chars']) // 10 + 10 * t['cyr_code'] + 30 * (foreign + len(intrude)) \
        + (500 if untranslated else 0) + (100 if cjk else 0)
    lost_chars = max(0, o['chars'] - t['chars'])
    # вставлять английский оригинал стоит, только если кода пропало заметно или есть подмена
    severe = foreign > 0 or bool(intrude) or (loss > 0.1 and max(lost_chars, missing * 4) >= 40)
    return {'total': total, 'missing': missing, 'extra': extra, 'loss': round(loss, 3), 'cyr_code': t['cyr_code'],
            'severe': severe, 'intrude': intrude,
            'foreign': foreign, 'untranslated': untranslated, 'cjk': cjk, 'bad': bad, 'score': score}


def find_foreign(tran, orig=None):
    pages = defaultdict(set)
    for i, info in tran.items():
        for l in {l for l, _ in info['lines']}:
            pages[l].add(i)
    # обычные фразы перед листингами («следующим образом:») и одиночные слова — не подмена
    out = {l for l, p in pages.items() if len(p) >= 4 and len(l.split()) >= 2 and not l.endswith(':')}
    # фразы на строках кода, повторяющиеся на 2+ страницах; хранятся с префиксом «~»
    runs = defaultdict(set)
    for i, info in tran.items():
        if orig and i in orig:
            for r, _ in code_runs(orig[i], info):
                runs[norm(r['text'])].add(i)
    return out | {'~' + t for t, p in runs.items() if len(p) >= 2 and len(t.split()) >= 2}


def check(orig_path, tran_path, code_font, orig=None):
    code_re = re.compile(code_font) if code_font else re.compile(r'$^')
    orig = orig or collect(orig_path, code_re)
    tran = collect(tran_path, code_re)
    foreign = find_foreign(tran, orig)
    res = {i + 1: score_page(orig[i], tran[i], foreign) for i in tran if i in orig}
    return res, foreign, orig


def summary(res, foreign):
    bad = [p for p, r in res.items() if r['bad']]
    lines = ['Проверено страниц: %d, проблемных: %d' % (len(res), len(bad))]
    for p in bad:
        r = res[p]
        why = []
        if r['loss'] > 0.1: why.append('потеряно ~%d%% кода' % round(r['loss'] * 100))
        if r['cyr_code']: why.append('русский текст в коде: %d' % r['cyr_code'])
        if r['foreign']: why.append('чужие строки: %d' % r['foreign'])
        if r.get('intrude'): why.append('текст в коде: «%s»' % '», «'.join(x[:40] for x in r['intrude'][:3]))
        if r['untranslated']: why.append('не переведена')
        if r['cjk']: why.append('китайские символы')
        lines.append('  стр. %d: %s' % (p, '; '.join(why)))
    full = sorted(l for l in foreign if not l.startswith('~'))
    runs = sorted(l[1:] for l in foreign if l.startswith('~'))
    if full:
        lines.append('Строки-подмены (на 4+ страницах): ' + ' | '.join(full))
    if runs:
        lines.append('Фразы на месте кода (на 2+ страницах): ' + ' | '.join(runs))
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
