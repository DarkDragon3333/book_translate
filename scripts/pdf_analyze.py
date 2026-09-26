#!/usr/bin/env python3
"""Разбор исходного PDF перед переводом.
- шрифт кода: моноширинные гарнитуры (Courier, *Mono*, Consolas…) по числу символов;
  результат — регулярка для pdf2zh --formular-font-pattern и qa_check.py;
- рекламные вставки: строки со ссылками (t.me, telegram, http…), повторяющиеся на 2+ страницах —
  от них в pdf2zh начинается «подмена кода», их лучше удалить из исходника до перевода;
- страницы без текстового слоя (скан).
Использование: python pdf_analyze.py book.pdf [--json out.json]
"""
import json, re, sys
from collections import Counter, defaultdict
import pdfplumber

MONO = re.compile(r'(Courier|Mono|Consol|Menlo|Inconsolata|Code|Fira|Letter ?Gothic|Lucida ?Console|Andale|Monaco|Typewriter)', re.I)
AD = re.compile(r'(t\.me/|telegram|телеграм|vk\.com/|https?://|www\.)', re.I)
AD_STRONG = re.compile(r'(t\.me/|telegram|телеграм|vk\.com/)', re.I)


def family(fontname):
    name = fontname.split('+', 1)[-1]
    return re.split(r'[-,]', name)[0]


def analyze(path):
    fonts, ads, empty = Counter(), defaultdict(set), []
    with pdfplumber.open(path) as pdf:
        n = len(pdf.pages)
        sample = set(range(0, n, max(1, n // 120))) | set(range(min(n, 15)))
        for i in range(n):
            page = pdf.pages[i]
            text = page.extract_text() or ''
            if len(text.strip()) < 20 and i < n - 1:
                empty.append(i + 1)
            for line in text.splitlines():
                if AD.search(line) and len(line.strip()) > 10:
                    ads[re.sub(r'\s+', ' ', line.strip())].add(i + 1)
            if i in sample:
                for ch in page.chars:
                    fonts[family(ch['fontname'])] += 1
            page.close()
    mono = [(f, c) for f, c in fonts.most_common()
            if MONO.search(f) and not re.search('unicode', f, re.I) and c > 50]
    pattern = '.*(%s).*' % '|'.join(re.escape(f) for f, _ in mono) if mono else ''
    # реклама: t.me/telegram на 2+ страницах; обычные ссылки — только если на 3+ страницах
    repeated = {l: sorted(p) for l, p in ads.items()
                if (AD_STRONG.search(l) and len(p) >= 2) or len(p) >= 3}
    return {'pages': n, 'code_fonts': mono, 'code_font_pattern': pattern,
            'ads': repeated, 'no_text_pages': empty,
            'top_fonts': fonts.most_common(8)}


if __name__ == '__main__':
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    r = analyze(sys.argv[1])
    if '--json' in sys.argv:
        json.dump(r, open(sys.argv[sys.argv.index('--json') + 1], 'w'), ensure_ascii=False, indent=1)
    print('    Страниц: %d' % r['pages'])
    print('    Шрифт кода: %s' % (', '.join('%s (%d симв.)' % x for x in r['code_fonts']) or 'не найден, беру formular_font_pattern из config.v3.toml'))
    if r['ads']:
        print('    ВНИМАНИЕ: повторяющиеся строки-ссылки (реклама?) — лучше удалить из исходника до перевода:')
        for l, p in list(r['ads'].items())[:10]:
            print('      стр. %s: %s' % (','.join(map(str, p[:12])), l[:90]))
    if len(r['no_text_pages']) > r['pages'] * 0.2:
        print('    ВНИМАНИЕ: %d страниц без текста — похоже на скан, нужен OCR' % len(r['no_text_pages']))
