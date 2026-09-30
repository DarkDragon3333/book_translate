#!/usr/bin/env python3
"""Смысл многозначных терминов по контексту: для каждого абзаца книги — какой перевод термина подсказать модели.

Порядок решения для каждого вхождения (по предложению, где стоит слово):
  1. употреблено глаголом («Recall that…») → ничего не подсказывать;
  2. подсказки в предложении: только чужого смысла → перевод другого смысла (или ничего), только своего → свой;
  3. лестница контекста по многословным терминам областей: абзац → соседи ±2 → раздел → глава;
     перевешивают «чужие» области → другой смысл, своя → свой;
  4. не решено → свой перевод (область включена для книги, значит книга про неё).
Вхождения в одном абзаце с разными решениями → ничего не подсказывать.
"""
import csv, os, re, sys
from collections import Counter, defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from build_glossary import plurals  # noqa: E402

MIN_EV, RATIO = 2, 2.0
SENT = re.compile(r'(?<=[.!?])\s+(?=[A-Z“"(])')


def load_senses(path):
    out = {}
    with open(path, encoding='utf-8') as f:
        for r in csv.DictReader(line for line in f if not line.startswith('#')):
            w = r['word'].strip()
            forms = [w] + plurals(w)
            out[w] = {
                'forms': re.compile(r'(?<!\w)(?:%s)(?!\w)' % '|'.join(map(re.escape, forms)), re.I),
                'own': set(r['own'].split()), 'own_target': r['own_target'],
                'conflict': set((r['conflict_domains'] or '').split()),
                'alt_target': r['alt_target'] or None,
                'alt': re.compile(r['alt_cues'], re.I) if r['alt_cues'] else None,
                'own_re': re.compile(r['own_cues'], re.I) if r['own_cues'] else None,
                'verb': re.compile(r['verb'], re.I) if r['verb'] else None,
                'default': (r.get('default') or 'own').strip()}
    return out


def load_multi(gdir):
    """Регулярки многословных терминов каждой области (кроме base и keep)."""
    res = {}
    for f in sorted(os.listdir(gdir)):
        if not f.endswith('.csv') or f in ('base.csv', 'keep.csv'):
            continue
        with open(os.path.join(gdir, f), encoding='utf-8-sig', newline='') as h:
            src = [r['source'].strip() for r in csv.DictReader(h) if (r.get('source') or '').strip()]
        forms = [x for s in src if ' ' in s or '-' in s for x in [s] + plurals(s)]
        low = [x for x in forms if x == x.lower()]
        cap = [x for x in forms if x != x.lower()]
        res[f[:-4]] = [re.compile(r'(?<!\w)(?:%s)(?!\w)' % '|'.join(map(re.escape, sorted(low, key=len, reverse=True))), re.I)
                       if low else None, re.compile(r'(?<!\w)(?:%s)(?!\w)' % '|'.join(map(re.escape, cap))) if cap else None]
    return res


def mcount(multi, text):
    c = Counter()
    for d, (rl, rc) in multi.items():
        n = (len(rl.findall(text)) if rl else 0) + (len(rc.findall(text)) if rc else 0)
        if n:
            c[d] = n
    return c


def ladder(levels, own, conflict):
    for L, cn in enumerate(levels):
        o = sum(cn.get(d, 0) for d in own)
        x = sum(cn.get(d, 0) for d in conflict)
        if o + x < MIN_EV:
            continue
        if o >= RATIO * x:
            return 'own', L
        if x >= RATIO * max(o, 1):
            return 'alt', L
    return None, None


def decide_paragraphs(paras, senses, multi, enabled):
    """paras: [{'text','ch','sec'}]. Возвращает {индекс абзаца: {слово: ('own'|'alt'|'none', перевод|None, причина)}}."""
    per = [mcount(multi, p['text']) for p in paras]
    sec_c, ch_c = defaultdict(Counter), defaultdict(Counter)
    for p, c in zip(paras, per):
        sec_c[p['sec']] += c
        ch_c[p['ch']] += c
    out = {}
    for i, p in enumerate(paras):
        for w, s in senses.items():
            if not (s['own'] & enabled) or not s['forms'].search(p['text']):
                continue
            near = Counter()
            for j in range(max(0, i - 2), min(len(paras), i + 3)):
                near += per[j]
            lad = None
            votes = []
            for sent in SENT.split(p['text']):
                if not s['forms'].search(sent):
                    continue
                if s['verb'] and s['verb'].search(sent):
                    votes.append(('none', 'глагол'))
                    continue
                a = bool(s['alt'] and s['alt'].search(sent))
                o = bool(s['own_re'] and s['own_re'].search(sent))
                if a and not o:
                    votes.append(('alt', 'предложение'))
                elif o and not a:
                    votes.append(('own', 'предложение'))
                elif s['default'] == 'none':          # слово решают только подсказки в предложении
                    votes.append(('none', 'нет подсказок'))
                else:
                    if lad is None:
                        lad = ladder([per[i], near, sec_c[p['sec']], ch_c[p['ch']]], s['own'], s['conflict'])
                    d, L = lad
                    votes.append((d or s['default'], ['абзац', 'соседи', 'раздел', 'глава'][L] if d else 'по умолчанию'))
            kinds = {v for v, _ in votes if v != 'none'} or {'none'}
            verbs = any(why == 'глагол' for _, why in votes)
            if len(kinds) > 1 or verbs and kinds != {'none'}:
                res = ('none', None, 'разные смыслы в абзаце')
            else:
                k = kinds.pop()
                tgt = s['own_target'] if k == 'own' else s['alt_target'] if k == 'alt' else None
                res = ('own' if k == 'own' else 'alt' if tgt else 'none', tgt, votes[0][1])
            out.setdefault(i, {})[w] = res
    return out


# ---------------- карта смыслов для конвейера ----------------
import json, zipfile
from html.parser import HTMLParser

_SKIP = {'pre', 'code', 'script', 'style', 'head', 'title', 'svg', 'math', 'var', 'sup', 'sub', 'nav'}
_BLOCK = {'p', 'li', 'dd', 'dt', 'figcaption', 'blockquote', 'td', 'th'}
_HEAD = {'h1', 'h2', 'h3', 'h4', 'h5', 'h6'}
_MARK = re.compile(r'⟦[^⟧]*⟧')


def key(text):
    """Ключ абзаца: только буквы в нижнем регистре, без меток bbm. Одинаков для текста из EPUB и из запроса bbm."""
    return re.sub(r'[^a-z]', '', _MARK.sub(' ', text).lower())


class _Paras(HTMLParser):
    def __init__(self):
        super().__init__()
        self.skip, self.cur, self.head, self.out = 0, [], False, []

    def handle_starttag(self, t, a):
        if t in _SKIP:
            self.skip += 1
        if t in _BLOCK | _HEAD:
            self.flush()
            self.head = t in _HEAD

    def handle_endtag(self, t):
        if t in _SKIP and self.skip:
            self.skip -= 1
        if t in _BLOCK | _HEAD:
            self.flush()

    def handle_data(self, d):
        if not self.skip:
            self.cur.append(d)

    def flush(self):
        t = re.sub(r'\s+', ' ', ''.join(self.cur)).strip()
        if len(t) > 1:
            self.out.append((self.head, t))
        self.cur, self.head = [], False


def lookup(smap, k, _idx={}):
    """Абзац в карте: точно по ключу, иначе по началу (bbm иногда режет абзац на части)."""
    if k in smap:
        return smap[k]
    if len(k) < 40:
        return None
    idx = _idx.get(id(smap))
    if idx is None:
        idx = _idx[id(smap)] = {}
        for kk in smap:
            idx.setdefault(kk[:40], []).append(kk)
    for kk in idx.get(k[:40], []):
        if kk.startswith(k) or k.startswith(kk):
            return smap[kk]
    return None


def epub_paragraphs(path):
    """[{'text','ch','sec'}] по порядку чтения; раздел — номер последнего заголовка."""
    z = zipfile.ZipFile(path)
    opf = re.search(r'full-path="([^"]+)"', z.read('META-INF/container.xml').decode('utf-8', 'replace')).group(1)
    o = z.read(opf).decode('utf-8', 'replace')
    base = os.path.dirname(opf)
    items = {}
    for m in re.finditer(r'<item\b([^>]*)/?>', o):
        a = dict(re.findall(r'([\w:-]+)="([^"]*)"', m.group(1)))
        items[a.get('id')] = a
    out, sec = [], 0
    for ch, idref in enumerate(re.findall(r'<itemref\b[^>]*idref="([^"]+)"', o)):
        a = items.get(idref) or {}
        if not a.get('href') or 'nav' in (a.get('properties') or ''):
            continue
        try:
            t = z.read(os.path.normpath(os.path.join(base, a['href'])).replace('\\', '/')).decode('utf-8', 'replace')
        except KeyError:
            continue
        p = _Paras()
        p.feed(t)
        p.flush()
        for head, txt in p.out:
            sec += head
            out.append({'text': txt, 'ch': ch, 'sec': sec})
    return out


def build_map(epub, rules, gdir, domains):
    senses, multi = load_senses(rules), load_multi(gdir)
    paras = epub_paragraphs(epub)
    dec = decide_paragraphs(paras, senses, multi, set(domains))
    m = {}
    for i, ws in dec.items():
        m[key(paras[i]['text'])] = {w: [k, t] for w, (k, t, _) in ws.items()}
    return m


if __name__ == '__main__':
    import argparse
    ap = argparse.ArgumentParser(description='Карта смыслов многозначных терминов для адаптера Rosetta')
    ap.add_argument('epub')
    ap.add_argument('out')
    ap.add_argument('--rules', default='/config/senses.csv')
    ap.add_argument('--glossary-dir', default='/config/glossary')
    ap.add_argument('--domains', default='')
    a = ap.parse_args()
    doms = [d for d in a.domains.split(',') if d and d != 'base']
    m = build_map(a.epub, a.rules, a.glossary_dir, doms)
    json.dump(m, open(a.out, 'w', encoding='utf-8'), ensure_ascii=False)
    c = Counter(k for ws in m.values() for k, _ in ws.values())
    print('    Смысл по контексту: абзацев со спорными словами %d; свой перевод %d, другой %d, не подсказывать %d'
          % (len(m), c['own'], c['alt'], c['none']))
