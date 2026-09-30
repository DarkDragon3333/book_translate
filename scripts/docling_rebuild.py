#!/usr/bin/env python3
"""PDF + разметка Docling → английский EPUB для EPUB-конвейера (bbm).

Docling отвечает, ГДЕ блок и ЧТО это (код, абзац, заголовок, рисунок, список) и в каком
порядке читать. Текст берётся из PDF (pdfplumber) по рамке блока: Docling теряет переводы
строк в коде, шрифты (inline-код, курсив) и типографику (— → -, ’ → ').

- код: строки и отступы по координатам глифов; выноски (другой шрифт) → нумерованный
  список после листинга, номер ❶ ставится у ближайшей строки кода; «Listing N.N» → подпись;
- проза: inline-код по шрифту кода → <code>, курсив → <em>, жирный → <strong>,
  переносы на концах строк снимаются (по словарю книги: copy-on-write остаётся);
- заголовки: уровень по нумерации (5.4 → h2, 5.5.1 → h3), остальные — по размеру шрифта;
  «заголовок» шрифтом основного текста → абзац;
- рисунки: вырезаются из страницы (200 dpi) вместе с подписями схем, которые Docling
  вынес наружу; «рисунок», почти весь набранный шрифтом кода, → листинг;
- метки страниц оригинала (epub:type="pagebreak") + page-list в оглавлении.

Использование:
  python docling_rebuild.py --pdf book.pdf --json book.json --out book_en.epub [--report r.txt]
      [--code-font REGEX] [--title "..."] [--dpi 200]
"""
import argparse, html, re, statistics, sys, uuid, zipfile, io
from collections import Counter, defaultdict
from datetime import datetime, timezone
import json
import pdfplumber

MONO = r'(Courier|Mono|Consol|Menlo|Inconsolata|Code|Fira|Letter ?Gothic|Lucida ?Console|Andale|Monaco|Typewriter)'
LIGA = {'ﬀ': 'ff', 'ﬁ': 'fi', 'ﬂ': 'fl', 'ﬃ': 'ffi', 'ﬄ': 'ffl', '\u00a0': ' ', '\u00ad': '-'}
CIRCLED = '❶❷❸❹❺❻❼❽❾❿⓫⓬⓭⓮⓯⓰⓱⓲⓳⓴'
LABEL_RE = re.compile(r'^(Figure|Listing|Table|Example)\s*(\d+[.\-]\d+)\.?\s*')
NUM_HEAD = re.compile(r'^(\d+(?:\.\d+)+)\s')
# реклама, вписанная в файл книги («Ещё больше книг … в телеграм канале», t.me/…) — выбрасывается
AD = re.compile(r'(\bt\.me/|\bvk\.com/|телеграм)', re.I)
HEAD_NUM = re.compile(r'^((?:[Aa]ppendix|APPENDIX)\s+[A-Z]|[A-Z]\.\d+(?:\.\d+)*|\d+(?:\.\d+)*)\.?\s+(?=\S)')
TOC_NAMES = {'contents', 'brief contents', 'table of contents'}   # печатное оглавление: в EPUB есть своё
INDEX_NAMES = {'index'}
IX_REFS = re.compile(r'(?<![\w.])(\d{1,4}(?:[–-]\d{1,4})?(?:,\s*\d{1,4}(?:[–-]\d{1,4})?)*)\s*$')
TOL = 1.0


def fam(fontname):
    return fontname.split('+', 1)[-1]


def is_pua(ch):
    return '\ue000' <= ch <= '\uf8ff'


def esc(s):
    return html.escape(s, quote=False)


class Book:
    def __init__(self, pdf_path, json_path, code_font, dpi):
        self.d = json.load(open(json_path, encoding='utf-8'))
        self.pdf = pdfplumber.open(pdf_path)
        self.code_re = re.compile(code_font, re.I)
        self.dpi = dpi
        self.pages = sorted(int(k) for k in self.d['pages'])
        self.stats = Counter()
        self.warn = []
        self.images = []          # (name, bytes)
        self._index()
        self._assign()
        self._styles()
        self._inline_code_back()
        self._vocab()

    # ---------- структура Docling ----------
    def node(self, ref):
        n = self.d
        for p in ref[2:].split('/'):
            n = n[int(p)] if p.isdigit() else n[p]
        return n

    def boxes(self, n):
        out = []
        for pr in n.get('prov', []):
            pg = pr['page_no']
            H = float(self.pdf.pages[pg - 1].height)
            b = pr['bbox']
            if b.get('coord_origin', 'BOTTOMLEFT') == 'BOTTOMLEFT':
                top, bot = H - b['t'], H - b['b']
            else:
                top, bot = b['t'], b['b']
            out.append((pg, b['l'], min(top, bot), b['r'], max(top, bot)))
        return out

    def _index(self):
        self.parent = {}
        self.items = []           # (ref, label, box)
        for kind in ('texts', 'pictures', 'tables'):
            for n in self.d.get(kind, []):
                self.parent[n['self_ref']] = n.get('parent', {}).get('$ref')
                lab = n.get('label', kind)
                for bx in self.boxes(n):
                    self.items.append((n['self_ref'], lab, bx))
        for g in self.d.get('groups', []):
            self.parent[g['self_ref']] = g.get('parent', {}).get('$ref')

    def picture_ancestor(self, ref):
        r = self.parent.get(ref)
        while r and r != '#/body':
            if r.startswith('#/pictures/'):
                return r
            r = self.parent.get(r)
        return None

    # ---------- символы → блоки ----------
    def _assign(self):
        """Каждый символ страницы — ровно одному блоку: самой маленькой рамке, содержащей центр."""
        self.owned = defaultdict(list)       # ref -> chars
        self.unassigned = defaultdict(list)  # page -> chars
        self.page_chars = {}
        byp = defaultdict(list)
        self.caption_refs = {c['$ref'] for kind in ('texts', 'pictures', 'tables')
                             for n in self.d.get(kind, []) for c in n.get('captions', [])}
        for ref, lab, (pg, l, t, r, b) in self.items:
            byp[pg].append(((r - l) * (b - t), ref, l, t, r, b))
        for pg in self.pages:
            page = self.pdf.pages[pg - 1]
            # только нужные поля: на 700 страницах полные словари pdfplumber занимают гигабайты
            chars = [{'text': c['text'], 'fontname': c['fontname'], 'size': c['size'], 'x0': c['x0'],
                      'x1': c['x1'], 'top': c['top'], 'bottom': c['bottom'], 'page': pg} for c in page.chars]
            page.close()
            self.page_chars[pg] = chars
            cand = sorted(byp[pg])
            for c in chars:
                x = (c['x0'] + c['x1']) / 2
                y = (c['top'] + c['bottom']) / 2
                for area, ref, l, t, r, b in cand:
                    if l - TOL <= x <= r + TOL and t - TOL <= y <= b + TOL:
                        pic = None if ref in self.caption_refs else self.picture_ancestor(ref)
                        self.owned[pic or ref].append(c)
                        break
                else:
                    if c['text'].strip():
                        self.unassigned[pg].append(c)

    def _inline_code_back(self):
        """Слово шрифтом кода в строке абзаца («…implementation of Ticket:»), попавшее в рамку соседнего
        листинга, возвращается в абзац: иначе в листинге появляется лишняя строка, а в абзаце — дыра."""
        if not self.body_style:
            return
        bodyf = self.body_style[0]
        prose = defaultdict(list)      # страница -> строки абзацев (верх, низ, x0, x1, ref)
        for ref, chars in self.owned.items():
            n = self.node(ref)
            if n.get('label') == 'code' or not ref.startswith('#/texts/'):
                continue
            for ln in self.lines(chars):
                body = [c for c in ln['chars'] if c['text'].strip() and fam(c['fontname']) == bodyf]
                if len(body) >= 3:
                    prose[ln['page']].append((ln['top'], ln['bottom'], ln['x0'], ln['x1'], ref))
        moved = 0
        for ref in [r for r in self.owned if r.startswith('#/texts/') and self.node(r).get('label') == 'code']:
            keep = []
            for c in self.owned[ref]:
                y, x = (c['top'] + c['bottom']) / 2, (c['x0'] + c['x1']) / 2
                hit = next((r2 for t, b, x0, x1, r2 in prose.get(c['page'], ())
                            if t - 0.5 <= y <= b + 0.5 and x0 - 2 <= x <= x1 + 2), None)
                if hit:
                    self.owned[hit].append(c)
                    moved += c['text'].strip() != ''
                else:
                    keep.append(c)
            self.owned[ref] = keep
        if moved:
            self.stats['inline-код из рамки листинга возвращён в абзац (символов)'] = moved

    def is_code(self, c):
        return bool(self.code_re.search(c['fontname']))

    def _styles(self):
        body, figure_font, in_code = Counter(), Counter(), Counter()
        font_total = Counter()
        for pg in self.pages:
            for c in self.page_chars[pg]:
                if c['text'].strip():
                    font_total[fam(c['fontname'])] += 1
        for ref, chars in self.owned.items():
            n = self.node(ref)
            lab = n.get('label')
            for c in chars:
                if not c['text'].strip():
                    continue
                f = fam(c['fontname'])
                if lab == 'text' and not self.is_code(c):
                    body[(f, round(c['size'], 1))] += 1
                if ref.startswith('#/pictures/'):
                    figure_font[f] += 1
                if lab == 'code' and not self.is_code(c) and not is_pua(c['text']):
                    in_code[f] += 1
        capf = Counter()
        for r in self.caption_refs:
            for c in self.owned.get(r, []):
                if c['text'].strip() and not self.is_code(c):
                    capf[fam(c['fontname'])] += 1
        self.caption_font = capf.most_common(1)[0][0] if capf else None
        self.body_style = body.most_common(1)[0][0] if body else None
        self.body_size = self.body_style[1] if self.body_style else 10
        # шрифты, почти целиком живущие внутри рисунков
        self.figure_fonts = {f for f, n in figure_font.items()
                             if n >= 20 and n >= 0.8 * font_total[f] and not self.code_re.search(f)}
        self.callout_font = None
        if in_code:
            f, n = in_code.most_common(1)[0]
            if n >= 20:
                self.callout_font = f
        self.figure_fonts.discard(self.callout_font)

    def dominant(self, chars):
        cnt = Counter((fam(c['fontname']), round(c['size'], 1)) for c in chars
                      if c['text'].strip() and not is_pua(c['text']))
        return cnt.most_common(1)[0][0] if cnt else (None, 0)

    # ---------- строки ----------
    @staticmethod
    def lines(chars):
        """Символы → строки (по вертикальному перекрытию), внутри строки — слева направо.
        Буквица (одна буква в 2,5+ раза крупнее текста) ставится в начало первой строки, которую она задевает:
        иначе её высота склеила бы две строки в одну («bTuild,h ties…»)."""
        out = []
        sizes = sorted(c['size'] for c in chars if c['text'].strip())
        med = sizes[len(sizes) // 2] if sizes else 0
        caps = [c for c in chars if med and c['size'] >= 2.5 * med and c['text'].isalpha() and len(sizes) > 20]
        if caps:
            ids = {id(c) for c in caps}
            chars = [c for c in chars if id(c) not in ids]
        for c in sorted(chars, key=lambda c: (c['page'], c['top'], c['x0'])):
            cy = (c['top'] + c['bottom']) / 2
            for ln in reversed(out[-3:]):
                if ln['page'] == c['page'] and ln['top'] - 0.5 <= cy <= ln['bottom'] + 0.5:
                    ln['chars'].append(c)
                    ln['top'] = min(ln['top'], c['top']) if c['text'].strip() else ln['top']
                    ln['bottom'] = max(ln['bottom'], c['bottom']) if c['text'].strip() else ln['bottom']
                    break
            else:
                out.append({'page': c['page'], 'top': c['top'], 'bottom': c['bottom'], 'chars': [c]})
        for cap in caps:
            for ln in out:
                if ln['page'] == cap['page'] and cap['top'] - 1 <= (ln['top'] + ln['bottom']) / 2 <= cap['bottom'] + 1:
                    ln['chars'].append(cap)
                    break
            else:
                out.append({'page': cap['page'], 'top': cap['top'], 'bottom': cap['bottom'], 'chars': [cap]})
        for ln in out:
            ln['chars'].sort(key=lambda c: c['x0'])
            vis = [c for c in ln['chars'] if c['text'].strip()]
            ln['x0'] = min((c['x0'] for c in vis), default=0)
            ln['x1'] = max((c['x1'] for c in vis), default=0)
        out = [ln for ln in out if any(c['text'].strip() for c in ln['chars'])]
        out.sort(key=lambda l: (l['page'], l['top']))
        return out

    def line_text(self, ln, keep_pua=False):
        s, prev = '', None
        for c in ln['chars']:
            t = LIGA.get(c['text'], c['text'])
            if not keep_pua and is_pua(t):
                prev = c
                continue
            if t == ' ':
                if s and not s.endswith(' '):
                    s += ' '
            else:
                if prev is not None and s and not s.endswith(' ') and c['x0'] - prev['x1'] > 0.15 * c['size']:
                    s += ' '
                s += t
            prev = c
        return s.strip()

    # ---------- стрелки выносок ----------
    def connectors(self, pg):
        """Тонкие линии и точки страницы → связные компоненты (стрелки выносок)."""
        if not hasattr(self, '_conn'):
            self._conn = {}
        if pg in self._conn:
            return self._conn[pg]
        page = self.pdf.pages[pg - 1]
        els = []
        objs = list(page.rects) + list(page.lines)
        curves = list(page.curves)
        page.close()
        for o in objs:
            w, h = o['x1'] - o['x0'], o['bottom'] - o['top']
            if min(w, h) <= 1.5 and max(w, h) <= 300:
                els.append((o['x0'], o['top'], o['x1'], o['bottom']))
        for o in curves:
            if o['x1'] - o['x0'] <= 8 and o['bottom'] - o['top'] <= 8:
                els.append((o['x0'], o['top'], o['x1'], o['bottom']))
        par = list(range(len(els)))

        def find(i):
            while par[i] != i:
                par[i] = par[par[i]]
                i = par[i]
            return i
        e = 1.5
        for i in range(len(els)):
            a = els[i]
            for j in range(i + 1, len(els)):
                b = els[j]
                if a[0] - e <= b[2] and b[0] - e <= a[2] and a[1] - e <= b[3] and b[1] - e <= a[3]:
                    par[find(i)] = find(j)
        comps = defaultdict(list)
        for i, el in enumerate(els):
            comps[find(i)].append(el)
        self._conn[pg] = list(comps.values())
        return self._conn[pg]

    def anchor_y(self, lines):
        """Высота строки кода, на которую указывает стрелка выноски (левый конец стрелки)."""
        pg = lines[0]['page']
        gx0 = min(l['x0'] for l in lines); gx1 = max(l['x1'] for l in lines)
        gt = min(l['top'] for l in lines); gb = max(l['bottom'] for l in lines)
        best = None
        for comp in self.connectors(pg):
            cx0 = min(e[0] for e in comp); cx1 = max(e[2] for e in comp)
            ct = min(e[1] for e in comp); cb = max(e[3] for e in comp)
            if cb < gt - 4 or ct > gb + 4:
                continue
            gap = max(0, gx0 - cx1, cx0 - gx1)
            if gap <= 12 and (best is None or gap < best[0]):
                best = (gap, comp)
        if not best:
            return None
        el = min(best[1], key=lambda e: (round(e[0] / 2), e[1]))
        return (el[1] + el[3]) / 2

    # ---------- словарь для переносов ----------
    def _vocab(self):
        self.vocab = Counter()
        for ref, chars in self.owned.items():
            for ln in self.lines(chars):
                words = self.line_text(ln).split()
                for w in words[:-1]:
                    w = re.sub(r'^[^\w]+|[^\w]+$', '', w.lower())
                    if w:
                        self.vocab[w] += 1

    def keep_hyphen(self, left, right):
        """left — слово перед переносом (с дефисом), right — первое слово следующей строки."""
        a = re.sub(r'^[^\w]+', '', left.lower())[:-1]
        b = re.sub(r'[^\w]+$', '', right.lower())
        joined, hyph = self.vocab[a + b], self.vocab[a + '-' + b]
        if hyph > joined:
            return True
        if joined > hyph:
            return False
        return '-' in a        # «copy-on-» + «write»: составное слово

    # ---------- inline-текст ----------
    def cls(self, c, line_size):
        if self.is_code(c):
            return 'code'
        f = fam(c['fontname'])
        if c['size'] < 0.75 * line_size and c['text'].strip() and re.match(r'[\d*†‡]', c['text']):
            return 'sup'
        bold = bool(re.search(r'Bold|Demi|Black|Heavy|Semibold', f, re.I))
        ital = bool(re.search(r'Ital|Oblique', f, re.I))
        return ('strong' if bold else '') + ('em' if ital else '') or 'p'

    def tokens(self, lines, plain=False, callout=False):
        """Строки → список (текст, класс) с пробелами и снятыми переносами."""
        toks = []
        for i, ln in enumerate(lines):
            vis = [c for c in ln['chars'] if c['text'].strip()]
            size = statistics.median(c['size'] for c in vis) if vis else self.body_size
            line_toks, prev = [], None
            for c in ln['chars']:
                t = LIGA.get(c['text'], c['text'])
                if is_pua(t):
                    self.stats['PUA-символы (маркеры списков)'] += 1
                    prev = c
                    continue
                k = self.cls(c, size)
                if plain and k not in ('code', 'sup'):
                    k = 'p'
                if callout and k in ('strong', 'em', 'strongem') and fam(c['fontname']) == self.callout_font:
                    k = 'p'
                if t == ' ':
                    if line_toks and line_toks[-1][0] != ' ':
                        line_toks.append((' ', k))
                elif prev is not None and line_toks and line_toks[-1][0] != ' ' and c['x0'] - prev['x1'] > 0.15 * c['size']:
                    line_toks.append((' ', k))
                    line_toks.append((t, k))
                else:
                    line_toks.append((t, k))
                prev = c
            while line_toks and line_toks[0][0] == ' ':
                line_toks.pop(0)
            while line_toks and line_toks[-1][0] == ' ':
                line_toks.pop()
            if not line_toks:
                continue
            if toks:
                last = ''.join(t for t, _ in toks[-40:]).split(' ')[-1]
                first = ''.join(t for t, _ in line_toks[:40]).split(' ')[0]
                code_break = toks[-1][1] == 'code' and line_toks[0][1] == 'code'
                if (toks[-1][0] in '-\u2010' and len(last) > 1 and last[-2].isalpha()
                        and first[:1].isalpha() and (first[:1].islower() or code_break)):
                    if self.keep_hyphen(last, first):
                        self.stats['переносы: дефис сохранён'] += 1
                        self.kept_hyphens.append(last + first)
                    else:
                        if toks[-1][1] == 'code':
                            self.stats['символы кода: снятые переносы'] += 1
                        toks.pop()
                        self.stats['переносы: слово склеено'] += 1
                elif toks[-1][0] in '—–/' or line_toks[0][0] in '—–':
                    pass
                else:
                    toks.append((' ', toks[-1][1] if toks[-1][1] == line_toks[0][1] else 'p'))
            toks.extend(line_toks)
        return toks

    def inline_html(self, lines, plain=False, callout=False):
        """plain=True — без курсива/жирного (заголовки, подписи), но inline-код сохраняется."""
        toks = self.tokens(lines, plain, callout)
        out, cur, buf = [], None, ''

        def flush():
            if not buf:
                return
            b = esc(buf)
            if cur == 'code':
                lead = len(buf) - len(buf.lstrip(' '))
                if lead:
                    out.append(' ')
                    b = esc(buf.lstrip(' '))
                self.stats['inline-код'] += 1
                self.stats['символы кода: inline <code>'] += len(re.sub(r'\s', '', buf))
                out.append('<code>%s</code>' % b)
            elif cur == 'sup':
                out.append('<sup>%s</sup>' % b)
            elif cur in ('em', 'strong', 'strongem'):
                s = b.strip()
                if not s:
                    out.append(b)
                else:
                    lead, trail = b[:len(b) - len(b.lstrip())], b[len(b.rstrip()):]
                    tag = '<strong><em>%s</em></strong>' if cur == 'strongem' else '<%s>%%s</%s>' % (cur, cur)
                    out.append(lead + tag % s + trail)
            else:
                out.append(b)

        for t, k in toks:
            if t == ' ' and cur in ('code', 'em', 'strong', 'strongem'):
                # пробел внутри кода/курсива не рвёт отрезок, если дальше тот же класс
                buf += t
                continue
            if k != cur:
                # хвостовой пробел отрезка выносим наружу
                trail = len(buf) - len(buf.rstrip(' '))
                if trail:
                    buf = buf.rstrip(' ')
                flush()
                if trail:
                    out.append(' ')
                cur, buf = k, ''
            buf += t
        trail = len(buf) - len(buf.rstrip(' '))
        buf = buf.rstrip(' ')
        flush()
        return re.sub(r'\s+', ' ', ''.join(out)).strip()

    def plain(self, chars):
        return ' '.join(self.line_text(ln) for ln in self.lines(chars)).strip()


# ---------- код ----------
def circled(i):
    return CIRCLED[i] if i < len(CIRCLED) else '(%d)' % (i + 1)


class Builder:
    def __init__(self, bk, title):
        self.bk = bk
        self.title = title
        self.blocks = []          # dict(kind, page, html, ...)
        self.pending_listing = None
        self.used = set()
        self.printed = self._printed_numbers()
        self.code_base = self._code_base()
        self.pic_no = 0
        self.head_levels = Counter()
        self.ad_pages = set()     # страницы с рекламой: картинки на них (QR-код канала) тоже выбрасываются
        self.toc_cache = {}       # страница → похожа на оглавление (toc_like)
        self.skip_mode = None     # 'toc' — печатное оглавление (выбрасывается), 'index' — предметный указатель
        self.skip_chars = []
        self.skip_pages = set()

    def _printed_numbers(self):
        m = {}
        for t in self.bk.d['texts']:
            if t.get('content_layer') == 'furniture' and re.fullmatch(r'\s*\d{1,4}\s*', t['text']):
                m[t['prov'][0]['page_no']] = int(t['text'])
        offs = Counter(pg - n for pg, n in m.items())
        off = offs.most_common(1)[0][0] if offs else None
        res = {}
        for pg in self.bk.pages:
            if pg in m:
                res[pg] = str(m[pg])
            elif off is not None and pg - off > 0:
                res[pg] = str(pg - off)
            else:
                res[pg] = 'pdf%d' % pg
        return res

    def _code_base(self):
        """Левый край кода отдельно для чётных и нечётных страниц (поля разные)."""
        per = defaultdict(Counter)
        for t in self.bk.d['texts']:
            if t['label'] != 'code':
                continue
            mins = {}
            for pg, ln in self._code_lines_raw(self.bk.owned.get(t['self_ref'], [])):
                mins[pg] = min(mins.get(pg, 1e9), ln['x0'])
            for pg, x in mins.items():
                per[pg % 2][round(x)] += 1
        return {k: min(v, key=lambda x: (-v[x], x)) for k, v in per.items() if v}

    def _code_lines_raw(self, chars):
        code = [c for c in chars if self.bk.is_code(c)]
        return [(ln['page'], ln) for ln in self.bk.lines(code)]

    # ----- блоки -----
    def add(self, kind, pages, html, **kw):
        self.blocks.append(dict(kind=kind, page=min(pages) if pages else None, pages=pages, html=html, **kw))

    def pages_of(self, n):
        return sorted({p['page_no'] for p in n.get('prov', [])})

    def walk(self, ref='#/body'):
        n = self.bk.node(ref)
        for ch in n.get('children', []):
            r = ch['$ref']
            if r in self.used:
                continue
            m = self.bk.node(r)
            if r.startswith('#/groups/') and self.skip_mode:
                self.collect(r)
                continue
            if r.startswith('#/groups/'):
                lab = m.get('label')
                if lab in ('list', 'ordered_list'):
                    self.do_list(m, r)
                elif lab == 'inline':
                    self.do_inline(m, r)
                else:
                    self.walk(r)
                continue
            if m.get('content_layer') == 'furniture':
                continue
            if self.skip_section(r):
                continue
            if r.startswith('#/pictures/'):
                self.do_picture(m, r)
            elif r.startswith('#/tables/'):
                self.do_table(m, r)
            else:
                self.do_text(m, r)
        return self

    def chars(self, ref):
        return self.bk.owned.get(ref, [])

    # ----- печатное оглавление и предметный указатель -----
    def big_text(self, ref):
        """Короткая надпись крупным шрифтом (заголовок главы/раздела книги) → текст, иначе None."""
        if ref.startswith('#/groups/'):
            return None
        vis = [c for c in self.chars(ref) if c['text'].strip() and not is_pua(c['text'])]
        if not vis or len(vis) > 80:
            return None
        font, size = self.bk.dominant(vis)
        if size >= 1.5 * self.bk.body_size:
            return re.sub(r'\s+', ' ', self.bk.plain(vis)).strip()
        return None

    def collect(self, ref):
        self.used.add(ref)
        n = self.bk.node(ref)
        for c in self.chars(ref):
            self.skip_chars.append(c)
            self.skip_pages.add(c['page'])
        for ch in n.get('children', []) + n.get('captions', []):
            if ch['$ref'] not in self.used:
                self.collect(ch['$ref'])

    def skip_section(self, ref):
        """Печатное оглавление (номера страниц бумажной книги) выбрасывается, предметный указатель
        собирается отдельно (без перевода). Раздел длится до следующего крупного заголовка."""
        bt = self.big_text(ref)
        name = (bt or '').lower()
        if (self.skip_mode and bt and len(re.findall(r'[A-Za-z]', bt)) >= 4
                and not (self.skip_mode == 'toc' and name in TOC_NAMES)
                and not (self.skip_mode == 'toc' and self.toc_like(ref))):
            self.end_skip()
        if not self.skip_mode and name in TOC_NAMES:
            self.skip_mode = 'toc'
            self.used.add(ref)
            return True
        if not self.skip_mode and name in INDEX_NAMES:
            self.skip_mode = 'index'
            self.used.add(ref)
            vis = [c for c in self.chars(ref) if c['text'].strip()]
            font, size = self.bk.dominant(vis)
            self.add('h', self.pages_of(self.bk.node(ref)), None, text='Index', style=(font, size), inner='Index')
            return True
        if self.skip_mode:
            self.collect(ref)
            return True
        return False

    def toc_like(self, ref):
        """Крупная строка на странице оглавления («PART 4 DEPLOYED SPRING ....385») — ещё оглавление, а не начало
        книги: на такой странице больше половины строк кончаются номером страницы (Spring in Action 30.09:
        оглавление 0.55–0.88, текст книги 0–0.2)."""
        pages = self.pages_of(self.bk.node(ref))
        if not pages:
            return False
        pg = pages[0]
        if pg not in self.toc_cache:
            try:
                lines = [l for l in (self.bk.pdf.pages[pg - 1].extract_text() or '').splitlines() if l.strip()]
            except Exception:
                lines = []
            ends = sum(1 for l in lines if re.search(r'\d+\s*$', l))
            self.toc_cache[pg] = len(lines) >= 5 and ends >= 0.5 * len(lines)
        return self.toc_cache[pg]

    def end_skip(self):
        mode, chars, pages = self.skip_mode, self.skip_chars, sorted(self.skip_pages)
        self.skip_mode, self.skip_chars, self.skip_pages = None, [], set()
        if mode == 'toc':
            self.bk.stats['печатное оглавление выброшено (стр.)'] += len(pages)
        elif mode == 'index' and chars:
            self.add('index', pages, self.render_index(chars))

    def render_index(self, chars):
        """Указатель по колонкам страницы: строка = статья; отступ → подстатья; строка из одних номеров
        страниц — продолжение предыдущей. Номера страниц становятся ссылками на метки страниц (в write_epub)."""
        bypage = defaultdict(list)
        for c in chars:
            bypage[c['page']].append(c)
        entries = []
        for pg in sorted(bypage):
            W = float(self.bk.pdf.pages[pg - 1].width)
            for col in ([c for c in bypage[pg] if (c['x0'] + c['x1']) / 2 < W / 2],
                        [c for c in bypage[pg] if (c['x0'] + c['x1']) / 2 >= W / 2]):
                ls = self.bk.lines(col)
                if not ls:
                    continue
                x0 = min(l['x0'] for l in ls)
                for ln in ls:
                    t = re.sub(r'\s+', ' ', self.bk.line_text(ln)).strip()
                    if not t:
                        continue
                    if entries and re.fullmatch(r'[\d,–\-\s]+', t) and re.search(r'\d', t):
                        pg0, lv0, t0 = entries[-1]
                        entries[-1] = (pg0, lv0, t0.rstrip(',') + (', ' if not t0.endswith(',') else ' ') + t)
                        continue
                    if entries and entries[-1][2].endswith(',') and ln['x0'] - x0 > 20:
                        pg0, lv0, t0 = entries[-1]
                        entries[-1] = (pg0, lv0, t0 + ' ' + t)
                        continue
                    lv = 0 if (len(t) == 1 and t.isalpha()) or t == 'Symbols' else (2 if ln['x0'] - x0 > 4 else 1)
                    entries.append((pg, lv, t))
        out = []
        for pg, lv, t in entries:
            m = IX_REFS.search(t)
            if m and lv:
                refs = re.sub(r'(\d{1,4})((?:[–-]\d{1,4})?)',
                              lambda r: '<a class="ixref" data-p="%s">%s</a>%s' % (r.group(1), r.group(1), esc(r.group(2))),
                              m.group(1))
                body = esc(t[:m.start()].rstrip()) + ' ' + refs
            else:
                body = esc(t)
            out.append('<p class="ix%d">%s</p>' % (lv, body))
        self.bk.stats['указатель: статей'] += len(entries)
        return '<div class="index" data-type="index" epub:type="index">\n%s\n</div>' % '\n'.join(out)

    def do_text(self, m, ref):
        lab = m.get('label')
        chars = self.chars(ref)
        pages = self.pages_of(m)
        if not any(c['text'].strip() for c in chars):
            if m.get('text', '').strip():
                self.bk.stats['блоки без символов в PDF (текст Docling)'] += 1
                self.add('p', pages, '<p>%s</p>' % esc(m['text']))
            return
        font, size = self.bk.dominant(chars)
        text = self.bk.plain(chars)
        if AD.search(text) and len(text) < 300:
            self.ad_pages.update(pages)
            self.bk.stats['реклама выброшена'] += 1
            self.bk.warn.append('стр. %s: реклама выброшена: %s' % (','.join(map(str, pages)), text[:70]))
            return
        if lab == 'code':
            cc = []
            for r in m.get('captions', []):
                self.used.add(r['$ref'])
                cc += self.chars(r['$ref'])
            if cc:
                self.pending_listing = (cc, pages)
            return self.do_code(chars, pages)
        if font in self.bk.figure_fonts:
            return self.attach_to_picture(chars, pages, text)
        vis = [c for c in chars if c['text'].strip() and not is_pua(c['text'])]
        if lab == 'text' and vis and sum(self.bk.is_code(c) for c in vis) >= 0.9 * len(vis):
            self.bk.stats['абзац целиком шрифтом кода → листинг'] += 1
            return self.do_code(chars, pages)
        if font == self.bk.callout_font and lab != 'section_header' and self.blocks and self.blocks[-1]['kind'] == 'code':
            return self.add_callout(self.blocks[-1], chars)
        if LABEL_RE.match(text) and text.startswith('Listing'):
            self.pending_listing = (chars, pages)
            return
        if lab == 'section_header' and (font, size) != self.bk.body_style:
            return self.heading(chars, pages, text, font, size)
        if lab == 'section_header':
            self.bk.stats['ложные заголовки → абзац'] += 1
        if lab == 'caption':
            return self.add('p', pages, '<p class="caption">%s</p>' % self.caption_html(chars))
        if lab == 'footnote':
            return self.add('p', pages, '<aside class="footnote" epub:type="footnote"><p>%s</p></aside>'
                            % self.bk.inline_html(self.bk.lines(chars)))
        if lab == 'formula':
            return self.add('p', pages, '<p class="formula"><code>%s</code></p>' % esc(text))
        if lab == 'list_item':
            return self.add('ul', pages, '<ul>\n<li><p>%s</p></li>\n</ul>' % self.bk.inline_html(self.bk.lines(chars)))
        self.check_code_lines(chars, pages)
        self.add('p', pages, '<p>%s</p>' % self.bk.inline_html(self.bk.lines(chars)))

    def check_code_lines(self, chars, pages):
        """Строка прозы целиком шрифтом кода — возможно, пропущенный листинг."""
        for ln in self.bk.lines(chars):
            vis = [c for c in ln['chars'] if c['text'].strip() and not is_pua(c['text'])]
            if len(vis) >= 3 and all(self.bk.is_code(c) for c in vis):
                self.bk.warn.append('стр. %d: строка кода внутри абзаца (защищена как <code>, но без переносов строк): %s'
                                    % (ln['page'], self.bk.line_text(ln)[:70]))

    def do_inline(self, g, ref):
        chars, pages = [], set()
        for ch in g.get('children', []):
            r = ch['$ref']
            self.used.add(r)
            chars += self.chars(r)
            pages |= set(self.pages_of(self.bk.node(r)))
        if chars:
            self.add('p', sorted(pages), '<p>%s</p>' % self.bk.inline_html(self.bk.lines(chars)))

    def do_list(self, g, ref):
        items, pages = [], set()
        ordered = g.get('label') == 'ordered_list'
        for ch in g.get('children', []):
            r = ch['$ref']
            m = self.bk.node(r)
            if r.startswith('#/groups/'):
                sub = Builder.__new__(Builder)
                sub.__dict__.update(self.__dict__)
                sub.blocks = []
                sub.walk(r)
                if sub.blocks:
                    if items:
                        items[-1] = items[-1][:-len('</li>')] + '\n' + '\n'.join(b['html'] for b in sub.blocks) + '</li>'
                    else:
                        items.append('<li>%s</li>' % '\n'.join(b['html'] for b in sub.blocks))
                continue
            if m.get('enumerated'):
                ordered = True
            chars = self.chars(r)
            pages |= set(self.pages_of(m))
            if m.get('label') == 'code':
                # фрагмент списка, принятый за код
                for ln in self.bk.lines(chars):
                    items.append('<li><p>%s</p></li>' % self.bk.inline_html([ln]))
                continue
            items.append('<li><p>%s</p></li>' % self.bk.inline_html(self.bk.lines(chars)))
        if items:
            tag = 'ol' if ordered else 'ul'
            self.add(tag, sorted(pages), '<%s>\n%s\n</%s>' % (tag, '\n'.join(items), tag))

    # ----- заголовки -----
    def heading(self, chars, pages, text, font, size):
        self.add('h', pages, None, text=text, style=(font, size),
                 inner=self.bk.inline_html(self.bk.lines(chars), plain=True))

    def resolve_headings(self):
        """Уровень: по нумерации «5.5.1», для прочих — по стилю/размеру относительно нумерованных."""
        style_level = defaultdict(Counter)
        for b in self.blocks:
            if b['kind'] == 'h':
                m = NUM_HEAD.match(b['text'])
                if m:
                    style_level[b['style']][m.group(1).count('.') + 1] += 1
        known = {s: c.most_common(1)[0][0] for s, c in style_level.items()}
        sizes = sorted(((s[1], lv) for s, lv in known.items()), reverse=True)
        maxsize = max([b['style'][1] for b in self.blocks if b['kind'] == 'h'] or [0])
        for b in self.blocks:
            if b['kind'] != 'h':
                continue
            m = NUM_HEAD.match(b['text'])
            if m:
                lv = m.group(1).count('.') + 1
            elif b['style'] in known:
                lv = known[b['style']]
            elif (b['style'][1] >= maxsize - 0.1 or b['style'][1] >= 1.5 * self.bk.body_size) and \
                    (not sizes or b['style'][1] > sizes[0][0] + 0.5):
                lv = 1          # крупнее всех нумерованных заголовков: главы, части, приложения
            else:
                bigger = [(sz, l) for sz, l in sizes if sz >= b['style'][1] - 0.05]
                if bigger:
                    sz, l = bigger[-1]
                    lv = l + (1 if b['style'][1] < sz - 0.3 else 0)
                else:
                    lv = (max(known.values()) if known else 2) + 1
            lv = max(1, min(6, lv))
            b['level'] = lv
        self.title_first()
        # номер главы нарисован картинкой — берём его из первого нумерованного заголовка главы («9.1 …» → «9 Kotlin»)
        hs = [b for b in self.blocks if b['kind'] == 'h']
        for i, b in enumerate(hs):
            if b['level'] != 1 or re.match(r'(\d|appendix|part|chapter)', b['text'], re.I):
                continue
            for nb in hs[i + 1:]:
                if nb['level'] == 1:
                    break
                m = re.match(r'^(\d+)\.\d', nb['text'])
                if m:
                    b['text'] = '%s %s' % (m.group(1), b['text'])
                    b['inner'] = '%s %s' % (m.group(1), b['inner'])
                    self.bk.stats['номер главы взят из нумерации разделов'] += 1
                    break
        for b in hs:
            lv = b['level']
            # номер раздела («A.2», «5.4.1», «appendix A», «9») — как подпись span.label: модель его не трогает
            # (Rosetta переделывала «A.2» в «2.2», «A.4» в «Глава 4.»)
            m = HEAD_NUM.match(b['inner'])
            inner = ('<span class="label">%s </span>%s' % (m.group(1), b['inner'][m.end():])) if m else b['inner']
            if m:
                self.bk.stats['номера заголовков защищены от перевода'] += 1
            b['html'] = '<h%d>%s</h%d>' % (lv, inner, lv)
            self.head_levels['h%d' % lv] += 1

    # ----- код -----
    def code_lines(self, chars, extra=()):
        code = [c for c in chars if self.bk.is_code(c) or id(c) in extra]
        lines = self.bk.lines(code)
        if not lines:
            return []
        widths = [c['x1'] - c['x0'] for c in code if c['text'].strip()]
        cw = statistics.median(widths) if widths else 6
        out = []
        for ln in lines:
            base = self.code_base.get(ln['page'] % 2, min(l['x0'] for l in lines))
            if ln['x0'] < base - cw / 2:
                base = min(l['x0'] for l in lines if l['page'] == ln['page'])
            s, pos = '', base
            for c in ln['chars']:
                if not c['text'].strip():
                    continue
                n = round((c['x0'] - pos) / cw)
                s += ' ' * max(n, 0 if s else 0) + LIGA.get(c['text'], c['text'])
                pos = c['x0'] + cw
            ln['code'] = s.rstrip()
            out.append(ln)
        return out

    def do_code(self, chars, pages, is_picture=False):
        # «Listing N.N …» в начале блока — подпись (вместе со словами шрифтом кода в ней)
        cap_lines = []
        for ln in self.bk.lines([c for c in chars if c['text'].strip() and not is_pua(c['text'])]):
            first = ln['chars'][0]
            if self.bk.is_code(first):
                break
            if not cap_lines and re.match(r'Listing\s*\d', self.bk.line_text(ln)):
                cap_lines.append(ln)
            elif cap_lines and fam(first['fontname']) == fam(cap_lines[0]['chars'][0]['fontname']):
                cap_lines.append(ln)
            else:
                break
        if cap_lines:
            ids = {id(c) for l in cap_lines for c in l['chars']}
            chars = [c for c in chars if id(c) not in ids]
            self.pending_listing = ([c for l in cap_lines for c in l['chars']], pages)
        # строки прозы (шрифт основного текста), которые Docling прихватил в блок кода сверху или снизу
        if not is_picture and self.bk.body_style:
            alll = self.bk.lines([c for c in chars if c['text'].strip() and not is_pua(c['text'])])
            bodyf = self.bk.body_style[0]

            def prose(ln):
                vis = [c for c in ln['chars'] if c['text'].strip()]
                if any(fam(c['fontname']) == self.bk.callout_font for c in vis):
                    return False
                body = [c for c in vis if fam(c['fontname']) == bodyf and re.match(r'[A-Za-z]', c['text'])]
                return len(body) >= 3
            head, tail = [], []
            while alll and prose(alll[0]):
                head.append(alll.pop(0))
            while alll and prose(alll[-1]):
                tail.insert(0, alll.pop())
            if head and not alll:
                self.bk.stats['«код» из строк прозы → абзац'] += 1
                return self.add('p', pages, '<p>%s</p>' % self.bk.inline_html(head))
            if (head or tail) and any(self.bk.is_code(c) for l in alll for c in l['chars']):
                ids = {id(c) for l in head + tail for c in l['chars']}
                self.bk.stats['строки прозы, отделённые от листинга'] += len(head) + len(tail)
                if head:
                    self.add('p', sorted({l['page'] for l in head}), '<p>%s</p>' % self.bk.inline_html(head))
                self.do_code([c for c in chars if id(c) not in ids], pages, is_picture)
                if tail:
                    self.add('p', sorted({l['page'] for l in tail}), '<p>%s</p>' % self.bk.inline_html(tail))
                return
        lines = self.code_lines(chars)
        inner = set()
        for ln in lines:
            for c in chars:
                if (not self.bk.is_code(c) and c['text'].strip() and not is_pua(c['text'])
                        and fam(c['fontname']) != self.bk.callout_font and c['page'] == ln['page']
                        and ln['top'] - 1 <= (c['top'] + c['bottom']) / 2 <= ln['bottom'] + 1
                        and ln['x0'] < c['x0'] and c['x1'] < ln['x1']):
                    inner.add(id(c))
        if inner:
            self.bk.stats['символы обычного шрифта внутри строк кода'] += len(inner)
            lines = self.code_lines(chars, inner)
        other = [c for c in chars if not self.bk.is_code(c) and c['text'].strip() and id(c) not in inner]
        # маркеры списка слева от кода → это список, а не листинг
        pua = [c for c in other if is_pua(c['text'])]
        if pua and lines and not is_picture:
            first_code_x = min(l['x0'] for l in lines)
            if all(c['x1'] <= first_code_x + 2 for c in pua):
                items = []
                for ln in self.bk.lines(chars):
                    h = self.bk.inline_html([ln])
                    if h:
                        items.append('<li><p>%s</p></li>' % h)
                self.bk.stats['«код» из пунктов списка → список'] += 1
                return self.add('ul', pages, '<ul>\n%s\n</ul>' % '\n'.join(items))
        # строка прозы между строками кода («or», «then») — делим листинг на части
        if self.split_interludes(chars, is_picture):
            return
        other_lines = self.bk.lines([c for c in other if not is_pua(c['text'])])
        caption, rest = cap_lines, other_lines
        if not lines:
            # в блоке нет шрифта кода — это текст
            if rest:
                self.bk.stats['«код» без шрифта кода → абзац'] += 1
                self.bk.warn.append('стр. %s: блок «код» без шрифта кода, выведен абзацем: %s'
                                    % (pages[0] if pages else '?', self.bk.plain([c for l in rest for c in l['chars']])[:70]))
            if rest:
                self.add('p', pages, '<p>%s</p>' % self.bk.inline_html(rest))
            return
        block = dict(kind='code', page=min(pages), pages=pages, lines=lines, callouts=[], html=None, picture=is_picture)
        # продолжение листинга на следующей странице
        prev = self.blocks[-1] if self.blocks else None
        if (prev and prev['kind'] == 'code' and not caption and self.pending_listing is None
                and prev['lines'] and lines and lines[0]['page'] == prev['lines'][-1]['page'] + 1
                and not is_picture):
            prev['lines'] += lines
            prev['pages'] = sorted(set(prev['pages']) | set(pages))
            self.bk.stats['листинги, склеенные через страницу'] += 1
            block = prev
        elif (prev and prev['kind'] == 'code' and not caption and self.pending_listing is None
                and not is_picture and not prev.get('picture') and self.adjacent(prev['lines'], lines)):
            self.merge_code(prev, lines)
            prev['pages'] = sorted(set(prev['pages']) | set(pages))
            self.bk.stats['обрывки листинга приклеены к соседнему листингу'] += 1
            block = prev
        elif not caption and not is_picture and len(lines) <= 3 and self.home_listing(lines) is not None:
            home = self.home_listing(lines)          # хвост строки из листинга выше («ring[]);» → «…String[]);»)
            self.merge_code(home, lines)
            self.bk.stats['обрывки листинга приклеены к соседнему листингу'] += 1
            block = home
        else:
            if self.pending_listing:
                pc, pp = self.pending_listing
                block['title'] = self.caption_html(pc)
                self.pending_listing = None
            self.blocks.append(block)
        for grp in self.group_callouts(rest):
            self.add_callout(block, None, grp)

    @staticmethod
    def adjacent(a, b):
        """Строки b — обрывок листинга a: на той же строке (Docling отрезал хвост строки) или вплотную
        выше/ниже (отдельная строка «^», «.», «}», продолжение команды)."""
        pg = b[0]['page']
        same = [l for l in a if l['page'] == pg]
        if not same:
            return False
        h = statistics.median([l['bottom'] - l['top'] for l in same + b]) or 8
        for x in same:
            for y in b:
                if x['top'] - 1 <= (y['top'] + y['bottom']) / 2 <= x['bottom'] + 1:
                    return True
        gap_below = b[0]['top'] - max(l['bottom'] for l in same)
        gap_above = min(l['top'] for l in same) - b[-1]['bottom']
        return -0.5 * h < gap_below < 1.6 * h or -0.5 * h < gap_above < 1.6 * h

    def home_listing(self, lines):
        """Более ранний листинг той же страницы, в строки которого попадает каждая строка обрывка."""
        for b in reversed(self.blocks[-8:]):
            if b['kind'] != 'code' or b.get('picture'):
                continue
            same = [l for l in b['lines'] if l['page'] == lines[0]['page']]
            if same and all(any(x['top'] - 1 <= (y['top'] + y['bottom']) / 2 <= x['bottom'] + 1 for x in same)
                            for y in lines):
                return b
        return None

    def merge_code(self, block, lines):
        keys = [(block['lines'][i]['page'], (block['lines'][i]['top'] + block['lines'][i]['bottom']) / 2)
                for i, _ in block['callouts']]
        chars = [c for l in block['lines'] + lines for c in l['chars']]
        block['lines'] = self.code_lines(chars, {id(c) for c in chars})
        new = []
        for (pg, y), (_, h) in zip(keys, block['callouts']):
            cand = [(abs((l['top'] + l['bottom']) / 2 - y), i) for i, l in enumerate(block['lines']) if l['page'] == pg]
            new.append((min(cand)[1] if cand else len(block['lines']) - 1, h))
        block['callouts'] = new

    def split_interludes(self, chars, is_picture):
        code = [c for c in chars if self.bk.is_code(c) and c['text'].strip()]
        if not code:
            return None
        cx0 = min(c['x0'] for c in code)
        key = lambda pg, y: (pg, y)
        ckeys = [key(c['page'], (c['top'] + c['bottom']) / 2) for c in code]
        inter = []
        for ln in self.bk.lines([c for c in chars if c['text'].strip() and not self.bk.is_code(c) and not is_pua(c['text'])]):
            f = fam(ln['chars'][0]['fontname'])
            k0, k1 = key(ln['page'], ln['top']), key(ln['page'], ln['bottom'])
            if (f != self.bk.callout_font and ln['x0'] < cx0 + 20
                    and any(k < k0 for k in ckeys) and any(k > k1 for k in ckeys)):
                inter.append(ln)
        if not inter:
            return None
        # слова шрифтом кода в строке прозы («…implementation of Ticket:») — часть этой строки, а не листинга
        full = []
        for ln in inter:
            extra = [c for c in code if c['page'] == ln['page'] and ln['top'] - 0.5 <= (c['top'] + c['bottom']) / 2 <= ln['bottom'] + 0.5]
            if extra:
                self.bk.stats['inline-код в строке прозы внутри листинга'] += 1
                ln = self.bk.lines(ln['chars'] + extra)[0] if len(self.bk.lines(ln['chars'] + extra)) == 1 else ln
            full.append(ln)
        inter = full
        ids = {id(c) for l in inter for c in l['chars']}
        segs = [[] for _ in range(len(inter) + 1)]
        for c in chars:
            if id(c) in ids:
                continue
            k = key(c['page'], (c['top'] + c['bottom']) / 2)
            segs[sum(1 for l in inter if key(l['page'], l['top']) < k)].append(c)
        self.bk.stats['листинги, разделённые строкой прозы'] += 1
        para = []                      # строки прозы подряд (без кода между ними) — один абзац

        def flush():
            if para:
                self.add('p', sorted({l['page'] for l in para}), '<p>%s</p>' % self.bk.inline_html(para))
                para.clear()
        for i, seg in enumerate(segs):
            if any(c['text'].strip() for c in seg):
                flush()
                self.do_code(seg, sorted({c['page'] for c in seg}), is_picture)
            if i < len(inter):
                para.append(inter[i])
        flush()
        return True

    def group_callouts(self, lines):
        groups = []
        for ln in lines:
            if groups:
                g = groups[-1][-1]
                h = g['bottom'] - g['top']
                if ln['page'] == g['page'] and ln['top'] - g['bottom'] < 0.8 * h and abs(ln['x0'] - groups[-1][0]['x0']) < 25:
                    groups[-1].append(ln)
                    continue
            groups.append([ln])
        return groups

    def add_callout(self, block, chars, lines=None):
        lines = lines or self.bk.lines(chars)
        if not lines:
            return
        cy = self.bk.anchor_y(lines)
        if cy is None:
            self.bk.stats['выноски без стрелки (номер по положению текста)'] += 1
            self.bk.warn.append('стр. %d: выноска без стрелки, номер поставлен по положению текста: %s'
                                % (lines[0]['page'], self.bk.line_text(lines[0])[:60]))
            cy = (lines[0]['top'] + lines[0]['bottom']) / 2
        cand = [(abs((l['top'] + l['bottom']) / 2 - cy), i) for i, l in enumerate(block['lines']) if l['page'] == lines[0]['page']]
        at = min(cand)[1] if cand else len(block['lines']) - 1
        block['callouts'].append((at, self.bk.inline_html(lines, callout=True)))
        self.bk.stats['выноски'] += 1

    def render_code(self, b):
        marks = defaultdict(list)
        order = sorted(range(len(b['callouts'])), key=lambda i: (b['callouts'][i][0], i))
        num = {}
        for k, i in enumerate(order):
            num[i] = k
            marks[b['callouts'][i][0]].append(circled(k))
        body = []
        for i, ln in enumerate(b['lines']):
            s = esc(ln['code'])
            if marks[i]:
                s += ' ' + ''.join('<var class="co">%s</var>' % m for m in marks[i])
            body.append(s)
        pre = '<pre data-type="programlisting" translate="no"><code>%s</code></pre>' % '\n'.join(body)
        parts = []
        if b.get('title'):
            parts.append('<div class="listing" data-type="example">\n<p class="listing-title">%s</p>' % b['title'])
        parts.append(pre)
        if b['callouts']:
            parts.append('<div class="callouts">\n%s\n</div>' % '\n'.join(
                '<p class="callout"><var class="co">%s</var> %s</p>' % (circled(num[i]), b['callouts'][i][1])
                for i in order))
        if b.get('title'):
            parts.append('</div>')
        self.bk.stats['листинги (pre)'] += 1
        self.bk.stats['строки кода'] += len(b['lines'])
        self.bk.stats['символы кода: в листингах'] += sum(len(re.sub(r'\s', '', l['code'])) for l in b['lines'])
        return '\n'.join(parts)

    def caption_html(self, chars):
        h = self.bk.inline_html(self.bk.lines(chars), plain=True)
        m = LABEL_RE.match(h)
        if m:
            label = '%s %s' % (m.group(1), m.group(2))
            return '<span class="label">%s </span>%s' % (label, h[m.end():])
        return h

    # ----- рисунки -----
    def do_picture(self, m, ref):
        chars = self.chars(ref)
        pages = self.pages_of(m)
        vis = [c for c in chars if c['text'].strip()]
        code = [c for c in vis if self.bk.is_code(c)]
        cap_refs = [c['$ref'] for c in m.get('captions', [])]
        cap_chars = []
        for r in cap_refs:
            self.used.add(r)
            cap_chars += self.chars(r)
        # подпись «Figure N.N», оказавшаяся внутри рисунка
        inner_cap = []
        if not cap_chars and self.bk.caption_font:
            # только символы шрифта подписей: иначе строка подписи слипается с надписями схемы
            ls = self.bk.lines([c for c in vis if fam(c['fontname']) == self.bk.caption_font])
            for i, ln in enumerate(ls):
                if re.match(r'(Figure|Table)\s*\d', self.bk.line_text(ln)):
                    inner_cap = [ln]
                    for l in ls[i + 1:]:
                        if l['top'] - inner_cap[-1]['bottom'] < 12:
                            inner_cap.append(l)
                    break
            cap_chars = [c for l in inner_cap for c in l['chars']]
            if cap_chars:
                self.bk.stats['подписи рисунков, найденные внутри рисунка'] += 1
        has_fig_caption = bool(cap_chars) and re.match(r'(Figure|Table)', self.bk.plain(cap_chars) or '')
        if len(code) >= 40 and len(code) >= 0.6 * len(vis) and not has_fig_caption:
            self.bk.stats['«рисунок» из кода → листинг'] += 1
            return self.do_code(chars, pages, is_picture=True)
        # заставка главы (большая цифра-картинка + название): текст крупным шрифтом → заголовок, картинка не нужна
        if vis and not cap_chars and len(vis) <= 80 and all(c['size'] >= 1.8 * self.bk.body_size for c in vis):
            self.bk.stats['заголовок внутри «рисунка» → заголовок'] += 1
            font, size = self.bk.dominant(vis)
            return self.heading(vis, pages, re.sub(r'\s+', ' ', self.bk.plain(vis)).strip(), font, size)
        pg, l, t, r, b = self.bk.boxes(m)[0]
        cap_ids = {id(c) for c in cap_chars}
        if cap_chars:
            # подпись под схемой внутри рамки рисунка — отрезаем (она будет переведена ниже)
            ctop = min(c['top'] for c in cap_chars)
            rest_bottom = max([c['bottom'] for c in vis if id(c) not in cap_ids] or [t])
            if t < ctop < b and ctop >= rest_bottom - 1:
                b = ctop - 2
        # подпись сбоку от схемы остаётся внутри кадра — закрасим её (перевод будет в figcaption)
        mask = None
        if cap_chars:
            m0 = (min(c['x0'] for c in cap_chars) - 1, min(c['top'] for c in cap_chars) - 1,
                  max(c['x1'] for c in cap_chars) + 1, max(c['bottom'] for c in cap_chars) + 1)
            if m0[1] < b and m0[3] > t and m0[0] < r and m0[2] > l:
                mask = m0
                self.bk.stats['подписи внутри кадра рисунка закрашены'] += 1
        self.pic_no += 1
        blk = dict(kind='figure', page=pg, pages=pages, box=[pg, l, t, r, b], mask=mask,
                   name='fig_%03d_%d.png' % (pg, self.pic_no),
                   caption=self.caption_html(cap_chars) if cap_chars else '', html=None)
        self.blocks.append(blk)
        self.bk.stats['рисунки'] += 1
        self.bk.stats['символы кода: на картинках рисунков'] += len(code)

    def attach_to_picture(self, chars, pages, text):
        pg = pages[0] if pages else None
        cand = [b for b in self.blocks[-6:] if b['kind'] == 'figure' and b['page'] == pg]
        x0 = min(c['x0'] for c in chars); x1 = max(c['x1'] for c in chars)
        t0 = min(c['top'] for c in chars); b0 = max(c['bottom'] for c in chars)
        if not cand:
            # рисунок может идти следом — отложим до render
            self.blocks.append(dict(kind='stray', page=pg, pages=pages, box=[pg, x0, t0, x1, b0], text=text, html=None))
            return
        f = cand[-1]
        bx = f['box']
        f['box'] = [bx[0], min(bx[1], x0 - 2), min(bx[2], t0 - 2), max(bx[3], x1 + 2), max(bx[4], b0 + 2)]
        self.bk.stats['подписи схем, возвращённые в рисунок'] += 1

    def render_figures(self):
        # «бродячие» подписи схем до рисунка на той же странице
        for i, b in enumerate(self.blocks):
            if b['kind'] != 'stray':
                continue
            figs = [f for f in self.blocks if f['kind'] == 'figure' and f['page'] == b['page']]
            if figs:
                f = min(figs, key=lambda f: abs(f['box'][2] - b['box'][2]))
                bx, sb = f['box'], b['box']
                f['box'] = [bx[0], min(bx[1], sb[1] - 2), min(bx[2], sb[2] - 2), max(bx[3], sb[3] + 2), max(bx[4], sb[4] + 2)]
                self.bk.stats['подписи схем, возвращённые в рисунок'] += 1
                b['html'] = ''
            else:
                self.bk.stats['текст шрифтом схем вне рисунка → абзац'] += 1
                b['html'] = '<p>%s</p>' % esc(b['text'])
        for b in self.blocks:
            if b['kind'] != 'figure':
                continue
            pg, l, t, r, bt = b['box']
            page = self.bk.pdf.pages[pg - 1]
            box = (max(0, l - 1), max(0, t - 1), min(float(page.width), r + 1), min(float(page.height), bt + 1))
            img = page.crop(box).to_image(resolution=self.bk.dpi)
            pic = img.original
            if b.get('mask'):
                from PIL import ImageDraw
                k = self.bk.dpi / 72.0
                x0, t0, x1, b1 = b['mask']
                ImageDraw.Draw(pic).rectangle([(x0 - box[0]) * k, (t0 - box[1]) * k, (x1 - box[0]) * k, (b1 - box[1]) * k],
                                              fill='white')
            buf = io.BytesIO()
            pic.save(buf, format='PNG', optimize=True)
            self.bk.images.append((b['name'], buf.getvalue()))
            cap = '\n<figcaption>%s</figcaption>' % b['caption'] if b['caption'] else ''
            b['html'] = '<figure>\n<img src="images/%s" alt=""/>%s\n</figure>' % (b['name'], cap)

    def cell_box(self, pg, bb):
        H = float(self.bk.pdf.pages[pg - 1].height)
        if bb.get('coord_origin', 'TOPLEFT') == 'BOTTOMLEFT':
            t, b = H - bb['t'], H - bb['b']
        else:
            t, b = bb['t'], bb['b']
        return bb['l'], min(t, b), bb['r'], max(t, b)

    def do_table(self, m, ref):
        """Структура (строки, столбцы, объединённые ячейки) — от Docling, текст ячеек — из PDF по рамке ячейки:
        так сохраняются типографика и inline-код (<code> защищает идентификаторы от перевода)."""
        data = m.get('data', {})
        cells = data.get('table_cells') or []
        pages = self.pages_of(m)
        if not cells:
            return self.do_picture(m, ref)
        vis = [c for c in self.chars(ref) if c['text'].strip() and not is_pua(c['text'])]
        if vis and sum(self.bk.is_code(c) for c in vis) >= 0.8 * len(vis) and not m.get('captions'):
            self.bk.stats['«таблица» из шрифта кода → листинг'] += 1
            return self.do_code(self.chars(ref), pages)
        pg = pages[0] if pages else None
        boxes = []
        for c in cells:
            bx = self.cell_box(pg, c['bbox']) if (pg and c.get('bbox')) else None
            boxes.append((((bx[2] - bx[0]) * (bx[3] - bx[1])) if bx else 0, bx))
        own = defaultdict(list)
        free = 0
        for ch in self.chars(ref):
            if ch['page'] != pg:
                continue
            x, y = (ch['x0'] + ch['x1']) / 2, (ch['top'] + ch['bottom']) / 2
            hit = [(area, i) for i, (area, bx) in enumerate(boxes)
                   if bx and bx[0] - TOL <= x <= bx[2] + TOL and bx[1] - TOL <= y <= bx[3] + TOL]
            if hit:
                own[min(hit)[1]].append(ch)
            elif ch['text'].strip():
                free += 1
        if free:
            self.bk.stats['символы таблиц вне ячеек'] += free
        nrows = max(c.get('end_row_offset_idx', 0) for c in cells)
        ncols = max(c.get('end_col_offset_idx', 0) for c in cells)
        covered = set()
        for c in cells:
            for rr in range(c.get('start_row_offset_idx', 0), c.get('end_row_offset_idx', 0)):
                for cc in range(c.get('start_col_offset_idx', 0), c.get('end_col_offset_idx', 0)):
                    covered.add((rr, cc))
        rows = []
        for r in range(nrows):
            tds = []
            # пустые ячейки Docling не выдаёт — без них столбцы съезжают
            row = [(c.get('start_col_offset_idx', 0), i, c) for i, c in enumerate(cells) if c.get('start_row_offset_idx') == r]
            row += [(cc, None, None) for cc in range(ncols) if (r, cc) not in covered]
            for _, i, c in sorted(row, key=lambda x: x[0]):
                if c is None:
                    tds.append('<td></td>')
                    continue
                tag = 'th' if c.get('column_header') else 'td'
                span = ''
                if c.get('row_span', 1) > 1:
                    span += ' rowspan="%d"' % c['row_span']
                if c.get('col_span', 1) > 1:
                    span += ' colspan="%d"' % c['col_span']
                if any(ch['text'].strip() for ch in own[i]):
                    body = self.bk.inline_html(self.bk.lines(own[i]))
                    self.bk.stats['ячейки таблиц: текст из PDF'] += 1
                else:
                    body = esc(c.get('text', ''))
                    if body:
                        self.bk.stats['ячейки таблиц: текст Docling'] += 1
                tds.append('<%s%s>%s</%s>' % (tag, span, body, tag))
            rows.append('<tr>%s</tr>' % ''.join(tds))
        cap = ''
        for r in m.get('captions', []):
            self.used.add(r['$ref'])
            cap = '<caption>%s</caption>' % self.caption_html(self.chars(r['$ref']))
        self.bk.stats['таблицы'] += 1
        self.add('table', pages, '<table>%s\n%s\n</table>' % (cap, '\n'.join(rows)))

    def merge_lists(self):
        out = []
        for b in self.blocks:
            if (out and b['kind'] == 'ul' and out[-1]['kind'] == 'ul'
                    and out[-1]['html'].endswith('</ul>') and b['html'].startswith('<ul>')):
                out[-1]['html'] = out[-1]['html'][:-len('</ul>')] + b['html'][len('<ul>\n'):]
                out[-1]['pages'] = sorted(set(out[-1]['pages']) | set(b['pages']))
                continue
            out.append(b)
        self.blocks = out

    @staticmethod
    def plain_html(h):
        return html.unescape(re.sub(r'<[^>]+>', '', h))

    def merge_pages(self):
        """Абзац, разорванный переходом страницы, — один абзац. Docling склеивает такие сам, но не на
        границе кусков (книга размечается кусками по 50 стр.) и не всегда, если внизу страницы сноска."""
        out = []
        for b in self.blocks:
            j = len(out) - 1
            while j >= 0 and (out[j].get('html') or '').startswith('<aside class="footnote"'):
                j -= 1                               # сноски внизу страницы не мешают склейке
            prev = out[j] if j >= 0 else None
            if (prev and b['kind'] == 'p' and prev['kind'] == 'p' and (b.get('html') or '').startswith('<p>')
                    and (prev.get('html') or '').startswith('<p>') and b['pages'] and prev['pages']
                    and min(b['pages']) == max(prev['pages']) + 1):
                left, right = self.plain_html(prev['html']).rstrip(), self.plain_html(b['html']).lstrip()
                if left and right and right[0].islower() and left[-1] not in '.!?:;)”"’':
                    ph, bh = prev['html'][3:-4].rstrip(), b['html'][3:-4].lstrip()
                    lw, rw = left.split()[-1], right.split()[0]
                    if left.endswith('-') and left[-2:-1].isalpha():
                        glue = ph if self.bk.keep_hyphen(lw, rw) else ph[:-1]
                        prev['html'] = '<p>%s%s</p>' % (glue, bh)
                    elif left[-1] in '—–/':
                        prev['html'] = '<p>%s%s</p>' % (ph, bh)     # «fail—» + «other»: без пробела
                    else:
                        prev['html'] = '<p>%s %s</p>' % (ph, bh)
                    prev['pages'] = sorted(set(prev['pages']) | set(b['pages']))
                    self.bk.stats['абзацы через страницу склеены'] += 1
                    continue
            out.append(b)
        self.blocks = out

    def title_first(self):
        """Название главы Docling иногда ставит после «This chapter covers» и первых абзацев страницы
        (название — картинка-заставка справа). Название переносится в начало своей страницы."""
        i = 0
        while i < len(self.blocks):
            b = self.blocks[i]
            if b['kind'] == 'h' and b.get('level') == 1 and b['page'] is not None:
                j = i
                while (j > 0 and self.blocks[j - 1]['page'] == b['page']
                       and not (self.blocks[j - 1]['kind'] == 'h' and self.blocks[j - 1].get('level') == 1)):
                    j -= 1
                if j < i:
                    self.blocks.insert(j, self.blocks.pop(i))
                    self.bk.stats['название главы перенесено в начало страницы'] += 1
            i += 1

    def fix_lists(self):
        """Маркеры, набранные текстом, дублируют маркер списка EPUB: «■ », «● » — убрать; «– » — вложенный
        список под предыдущим пунктом; <sup>1</sup>, <sup>2</sup>… по порядку — нумерованный список."""
        for b in self.blocks:
            if b['kind'] != 'ul' or not b.get('html') or not b['html'].startswith('<ul>'):
                continue
            items = re.findall(r'<li>(.*?)</li>', b['html'], re.S)
            if not items:
                continue
            nums = [re.match(r'<p><sup>(\d+)</sup>\s*', it) for it in items]
            if all(nums) and [int(m.group(1)) for m in nums] == list(range(1, len(items) + 1)):
                b['html'] = '<ol>\n%s\n</ol>' % '\n'.join('<li><p>%s</li>' % it[m.end():] for it, m in zip(items, nums))
                self.bk.stats['нумерованные списки'] += 1
                continue
            out = []
            for it in items:
                it2 = re.sub(r'^<p>[■●•▪]\s*', '<p>', it)
                m = re.match(r'<p>[–—-]\s+', it2)
                if m and out:
                    sub = '<li><p>%s</li>' % it2[m.end():]
                    if out[-1].endswith('</ul>'):
                        out[-1] = out[-1][:-len('</ul>')] + sub + '</ul>'
                    else:
                        out[-1] += '<ul>%s</ul>' % sub
                    self.bk.stats['пункты «–» → вложенный список'] += 1
                    continue
                if it2 != it:
                    self.bk.stats['маркеры-символы в пунктах списка убраны'] += 1
                out.append(it2)
            b['html'] = '<ul>\n%s\n</ul>' % '\n'.join('<li>%s</li>' % x for x in out)

    def finish(self):
        if self.skip_mode:
            self.end_skip()
        if self.ad_pages:
            n = len(self.blocks)
            self.blocks = [b for b in self.blocks if not (b['kind'] == 'figure' and b['page'] in self.ad_pages
                                                          and not b.get('caption'))]
            self.bk.stats['картинки на страницах с рекламой выброшены'] += n - len(self.blocks)
        self.merge_lists()
        self.fix_lists()
        self.merge_pages()
        if self.pending_listing:
            pc, pp = self.pending_listing
            self.add('p', pp, '<p class="listing-title">%s</p>' % self.caption_html(pc))
        self.resolve_headings()
        self.render_figures()
        for b in self.blocks:
            if b['kind'] == 'code':
                b['html'] = self.render_code(b)


# ---------- EPUB ----------
CSS = """body { font-family: serif; line-height: 1.4; }
h1, h2, h3, h4, h5, h6 { font-family: sans-serif; }
pre { font-family: monospace; font-size: 0.85em; white-space: pre-wrap; background: #f4f4f4; padding: 0.5em; }
code { font-family: monospace; }
var.co { font-style: normal; color: #1a5fb4; }
div.callouts { margin: 0.3em 0 1em 0; }
p.callout { font-size: 0.9em; margin: 0.2em 0 0.2em 1.6em; text-indent: -1.6em; }
pre, figure { page-break-inside: avoid; break-inside: avoid; }
p.listing-title, figcaption, p.caption { font-family: sans-serif; font-size: 0.9em; font-weight: bold; }
figure { margin: 1em 0; text-align: center; }
figure img { max-width: 100%; }
aside.footnote { font-size: 0.85em; }
table { border-collapse: collapse; margin: 1em 0; font-size: 0.9em; }
td, th { border: 1px solid #999; padding: 0.2em 0.4em; vertical-align: top; }
caption { font-family: sans-serif; font-weight: bold; text-align: left; }
div.index p { margin: 0; }
p.ix0 { font-weight: bold; margin-top: 0.8em !important; }
p.ix2 { margin-left: 1.5em !important; }
"""

XHTML = """<?xml version="1.0" encoding="utf-8"?>
<!DOCTYPE html>
<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops" lang="en" xml:lang="en">
<head><meta charset="utf-8"/><title>%s</title><link rel="stylesheet" type="text/css" href="style.css"/></head>
<body>
<section data-type="chapter">
%s
</section>
</body>
</html>
"""


def write_epub(bld, out, title):
    bk = bld.bk
    chapters, cur = [], None
    seen_pages, used_ids = set(), set()
    order = []
    for b in bld.blocks:
        if not b.get('html'):
            continue
        if b['kind'] == 'h' and b.get('level') == 1 and cur is not None and not cur['content']:
            pass                   # «Part 1» + «From 8 to 11…»: заголовки подряд — в одном файле
        elif b['kind'] == 'h' and b.get('level') == 1 or cur is None:
            cur = {'title': None, 'parts': [], 'heads': [], 'content': False}
            chapters.append(cur)
        if b['kind'] != 'h':
            cur['content'] = True
        marks = ''
        for pg in (b.get('pages') or [b['page']]):
            if pg is not None and pg not in seen_pages:
                seen_pages.add(pg)
                num = bld.printed[pg]
                pid = 'page_%s' % num
                while pid in used_ids:
                    pid += '_'
                used_ids.add(pid)
                marks += '<span epub:type="pagebreak" role="doc-pagebreak" id="%s" title="%s"></span>\n' % (pid, num)
                order.append((num, pid, len(chapters) - 1))
        if b['kind'] == 'h':
            hid = 'h%d' % (sum(len(c['heads']) for c in chapters) + 1)
            b['html'] = b['html'].replace('<h%d>' % b['level'], '<h%d id="%s">' % (b['level'], hid), 1)
            cur['heads'].append((b['level'], hid, b['text']))
            if cur['title'] is None:
                cur['title'] = b['text']
        cur['parts'].append(marks + b['html'])
    bk.stats['метки страниц'] = len(order)
    # номера страниц в указателе → ссылки на метки страниц оригинала
    pmap = {}
    for num, pid, ci in order:
        pmap.setdefault(num, 'ch%03d.xhtml#%s' % (ci, pid))
    linked = Counter()

    def ixlink(m):
        href = pmap.get(m.group(1))
        linked[bool(href)] += 1
        return '<a href="%s">' % href if href else '<a>'
    for ch in chapters:
        ch['parts'] = [re.sub(r'<a class="ixref" data-p="(\d+)">', ixlink, x) if 'ixref' in x else x for x in ch['parts']]
    if linked:
        bk.stats['указатель: номера страниц → ссылки'] = linked[True]
        bk.stats['указатель: номера без страницы'] = linked[False]
    files = []
    for i, ch in enumerate(chapters):
        name = 'ch%03d.xhtml' % i
        files.append((name, ch))
    uid = 'urn:uuid:%s' % uuid.uuid4()
    now = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
    z = zipfile.ZipFile(out, 'w')
    z.writestr(zipfile.ZipInfo('mimetype'), 'application/epub+zip', compress_type=zipfile.ZIP_STORED)
    zc = dict(compress_type=zipfile.ZIP_DEFLATED)
    z.writestr('META-INF/container.xml', '<?xml version="1.0" encoding="UTF-8"?>\n<container version="1.0" '
               'xmlns="urn:oasis:names:tc:opendocument:xmlns:container"><rootfiles><rootfile full-path="OEBPS/content.opf" '
               'media-type="application/oebps-package+xml"/></rootfiles></container>', **zc)
    z.writestr('OEBPS/style.css', CSS, **zc)
    for name, ch in files:
        z.writestr('OEBPS/' + name, XHTML % (esc(ch['title'] or title), '\n'.join(ch['parts'])), **zc)
    for name, data in bk.images:
        z.writestr('OEBPS/images/' + name, data, **zc)
    # оглавление: h1 → h2 вложенно
    toc = []
    for name, ch in files:
        heads = ch['heads'] or [(1, None, ch['title'] or title)]
        stack = []
        for lv, hid, text in heads:
            if lv > 2:
                continue
            href = name + ('#' + hid if hid else '')
            toc.append((lv, href, text))
    nav_items, open_sub = [], False
    for lv, href, text in toc:
        if lv == 1 or not nav_items:
            if open_sub:
                nav_items.append('</ol></li>'); open_sub = False
            elif nav_items:
                nav_items.append('</li>')
            nav_items.append('<li><a href="%s">%s</a>' % (href, esc(text)))
        else:
            if not open_sub:
                nav_items.append('<ol>'); open_sub = True
            nav_items.append('<li><a href="%s">%s</a></li>' % (href, esc(text)))
    if nav_items:
        nav_items.append('</ol></li>' if open_sub else '</li>')
    pl = '\n'.join('<li><a href="%s#%s">%s</a></li>' % (files[ci][0], pid, num) for num, pid, ci in order)
    nav = ('<?xml version="1.0" encoding="utf-8"?>\n<!DOCTYPE html>\n<html xmlns="http://www.w3.org/1999/xhtml" '
           'xmlns:epub="http://www.idpf.org/2007/ops" lang="en" xml:lang="en"><head><meta charset="utf-8"/><title>%s</title></head><body>\n'
           '<nav epub:type="toc" id="toc"><h1>Contents</h1><ol>\n%s\n</ol></nav>\n'
           '<nav epub:type="page-list" hidden=""><h1>Pages</h1><ol>\n%s\n</ol></nav>\n</body></html>'
           % (esc(title), '\n'.join(nav_items), pl))
    z.writestr('OEBPS/nav.xhtml', nav, **zc)
    ncx_points = '\n'.join('<navPoint id="np%d" playOrder="%d"><navLabel><text>%s</text></navLabel><content src="%s"/></navPoint>'
                           % (i + 1, i + 1, esc(t), h) for i, (lv, h, t) in enumerate([x for x in toc if x[0] == 1] or toc))
    z.writestr('OEBPS/toc.ncx', '<?xml version="1.0" encoding="utf-8"?>\n<ncx xmlns="http://www.daisy.org/z3986/2005/ncx/" '
               'version="2005-1"><head><meta name="dtb:uid" content="%s"/></head><docTitle><text>%s</text></docTitle>'
               '<navMap>%s</navMap></ncx>' % (uid, esc(title), ncx_points), **zc)
    manifest = ['<item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>',
                '<item id="ncx" href="toc.ncx" media-type="application/x-dtbncx+xml"/>',
                '<item id="css" href="style.css" media-type="text/css"/>']
    for i, (name, ch) in enumerate(files):
        manifest.append('<item id="c%d" href="%s" media-type="application/xhtml+xml"/>' % (i, name))
    for i, (name, _) in enumerate(bk.images):
        manifest.append('<item id="img%d" href="images/%s" media-type="image/png"/>' % (i, name))
    spine = '\n'.join('<itemref idref="c%d"/>' % i for i in range(len(files)))
    opf = ('<?xml version="1.0" encoding="utf-8"?>\n<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="uid">'
           '<metadata xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:identifier id="uid">%s</dc:identifier>'
           '<dc:title>%s</dc:title><dc:language>en</dc:language><meta property="dcterms:modified">%s</meta>'
           '<dc:source>docling_rebuild</dc:source></metadata>\n<manifest>\n%s\n</manifest>\n<spine toc="ncx">\n%s\n</spine></package>'
           % (uid, esc(title), now, '\n'.join(manifest), spine))
    z.writestr('OEBPS/content.opf', opf, **zc)
    z.close()
    return len(files)


# ---------- проверка ----------
def report(bld, nfiles):
    bk = bld.bk
    code_total = code_pre = code_inline = code_img = 0
    kinds = {}
    for ref, chars in bk.owned.items():
        n = bk.node(ref)
        if n.get('content_layer') == 'furniture':
            continue
        for c in chars:
            if c['text'].strip() and bk.is_code(c):
                code_total += 1
    lost = sum(len(v) for v in bk.unassigned.values())
    lost_code = sum(1 for v in bk.unassigned.values() for c in v if bk.is_code(c))
    out = []
    p = out.append
    p('Страницы PDF: %d–%d (%d)' % (bk.pages[0], bk.pages[-1], len(bk.pages)))
    p('Шрифт основного текста: %s %s; шрифт выносок: %s; шрифты схем: %s'
      % (bk.body_style[0], bk.body_style[1], bk.callout_font or '—', ', '.join(sorted(bk.figure_fonts)) or '—'))
    p('Глав (файлов): %d; заголовки: %s' % (nfiles, ', '.join('%s=%d' % kv for kv in sorted(bld.head_levels.items()) if kv[1])))
    for k in sorted(bk.stats):
        p('%s: %d' % (k, bk.stats[k]))
    placed = sum(v for k, v in bk.stats.items() if k.startswith('символы кода:'))
    p('Символов шрифта кода (без колонтитулов): %d; размещено: %d; не как код: %d; вне блоков Docling: %d'
      % (code_total, placed, code_total - placed, lost_code))
    if code_total - placed > 0.002 * code_total:
        bk.warn.append('часть символов шрифта кода попала в текст не как код: %d' % (code_total - placed))
    p('Символов вне всех блоков Docling (не попали в книгу): %d' % lost)
    for pg, v in sorted(bk.unassigned.items()):
        s = ''.join(c['text'] for c in sorted(v, key=lambda c: (round(c['top']), c['x0'])))
        p('    стр. %d: %s' % (pg, s[:100]))
    if bld.bk.kept_hyphens:
        p('Переносы, где дефис сохранён: ' + ', '.join(sorted(set(bld.bk.kept_hyphens))[:40]))
    if bk.warn:
        p('ВНИМАНИЕ (%d):' % len(bk.warn))
        for w in bk.warn:
            p('    ' + w)
    return '\n'.join(out)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--pdf', required=True)
    ap.add_argument('--json', required=True)
    ap.add_argument('--out', required=True)
    ap.add_argument('--report')
    ap.add_argument('--code-font', default=MONO)
    ap.add_argument('--title')
    ap.add_argument('--dpi', type=int, default=200)
    a = ap.parse_args()
    bk = Book(a.pdf, a.json, a.code_font, a.dpi)
    bk.kept_hyphens = []
    meta = (bk.pdf.metadata or {}).get('Title')
    title = a.title or (meta.strip() if isinstance(meta, str) and meta.strip() else None) or bk.d.get('name') or 'Book'
    bld = Builder(bk, title).walk()
    bld.finish()
    n = write_epub(bld, a.out, title)
    r = report(bld, n)
    print(r)
    if a.report:
        open(a.report, 'w', encoding='utf-8').write(r + '\n')


if __name__ == '__main__':
    main()
