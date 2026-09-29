#!/usr/bin/env python3
"""Разбор исходного PDF перед переводом.
- шрифт кода: моноширинный шрифт — по названию (Courier, *Mono*, Consolas…) или по тому, что все его символы
  одной ширины (так находятся mplus1mn в Pro Git и подобные); результат — регулярка для pdf2zh
  --formular-font-pattern, qa_pdf.py и docling_rebuild.py --code-font;
- рекламные вставки: строки с t.me / telegram / vk.com, повторяющиеся на 2+ страницах —
  от них в pdf2zh начинается «подмена кода», pdf_clean.py вырезает их до перевода.
  Обычные ссылки (http…) — часть книги, в том числе строки кода, их не трогаем;
- страницы без текстового слоя (скан).
Использование: python pdf_analyze.py book.pdf [--json out.json] [--fonts-only]
  --fonts-only — только шрифт кода (по выборке страниц, быстро), без поиска рекламы и сканов.
"""
import json, re, sys
from collections import Counter, defaultdict
import pdfplumber

MONO = re.compile(r'(Courier|Mono|Consol|Menlo|Inconsolata|Code|Fira|Letter ?Gothic|Lucida ?Console|Andale|Monaco|Typewriter)', re.I)
NOT_TEXT = re.compile(r'(Math|Symbol|Awesome|Wingding|Dingbat|Icon|Emoji|unicode)', re.I)
AD = re.compile(r'(\bt\.me/|telegram|телеграм|\bvk\.com/)', re.I)


def font_name(fontname):
    """Имя шрифта без префикса подмножества: 'ABCDEF+mplus1mn-regular' -> 'mplus1mn-regular'."""
    return fontname.split('+', 1)[-1]


def is_mono(name, count, same_width):
    """Моноширинный шрифт кода: по названию или по одинаковой ширине почти всех символов."""
    if NOT_TEXT.search(name):
        return False
    if MONO.search(name):
        return count > 50
    return count >= 200 and same_width >= 0.95


def analyze(path, fonts_only=False):
    fonts, widths, ads, empty = Counter(), defaultdict(Counter), defaultdict(set), []
    with pdfplumber.open(path) as pdf:
        n = len(pdf.pages)
        sample = set(range(0, n, max(1, n // 120))) | set(range(min(n, 15)))
        for i in (sorted(sample) if fonts_only else range(n)):
            page = pdf.pages[i]
            if not fonts_only:
                text = page.extract_text() or ''
                if len(text.strip()) < 20 and i < n - 1:
                    empty.append(i + 1)
                for line in text.splitlines():
                    if AD.search(line) and len(line.strip()) > 10:
                        ads[re.sub(r'\s+', ' ', line.strip())].add(i + 1)
            if i in sample:
                for ch in page.chars:
                    name = font_name(ch['fontname'])
                    fonts[name] += 1
                    if ch['text'].strip() and ch['size'] > 0:
                        widths[name][round((ch['x1'] - ch['x0']) / ch['size'], 2)] += 1
            page.close()
    same = {}
    for name, w in widths.items():
        total = sum(w.values())
        mode = w.most_common(1)[0][0] if w else 0
        same[name] = sum(c for x, c in w.items() if abs(x - mode) <= 0.02) / total if total else 0
    mono = [(f, c) for f, c in fonts.most_common() if is_mono(f, c, same.get(f, 0))]
    pattern = '.*(%s).*' % '|'.join(re.escape(f) for f, _ in mono) if mono else ''
    return {'pages': n, 'code_fonts': mono, 'code_font_pattern': pattern,
            'ads': {l: sorted(p) for l, p in ads.items() if len(p) >= 2}, 'no_text_pages': empty,
            'top_fonts': [(f, c, round(same.get(f, 0), 2)) for f, c in fonts.most_common(8)]}


if __name__ == '__main__':
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    r = analyze(sys.argv[1], '--fonts-only' in sys.argv)
    if '--json' in sys.argv:
        json.dump(r, open(sys.argv[sys.argv.index('--json') + 1], 'w'), ensure_ascii=False, indent=1)
    print('    Страниц: %d' % r['pages'])
    print('    Шрифт кода: %s' % (', '.join('%s (%d симв.)' % x for x in r['code_fonts'])
                                  or 'не найден, беру formular_font_pattern из config.v3.toml'))
    if r['ads']:
        print('    ВНИМАНИЕ: реклама (t.me, telegram, vk) на нескольких страницах — будет вырезана до перевода:')
        for l, p in list(r['ads'].items())[:10]:
            print('      стр. %s: %s' % (','.join(map(str, p[:12])), l[:90]))
    if len(r['no_text_pages']) > r['pages'] * 0.2:
        print('    ВНИМАНИЕ: %d страниц без текста — похоже на скан, нужен OCR' % len(r['no_text_pages']))
