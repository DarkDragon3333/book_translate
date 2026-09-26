#!/usr/bin/env python3
"""Вырезает рекламные строки (найденные pdf_analyze.py) из рабочей копии PDF до перевода.
Именно с перевода таких строк у pdf2zh начинается «подмена кода» в длинном прогоне.
Использует PyMuPDF (есть в образе pdf2zh). При любой ошибке копирует файл без изменений
и предупреждает — перевод не останавливается.
Использование: python pdf_clean.py in.pdf out.pdf analysis.json
"""
import json, re, shutil, sys

AD_STRONG = re.compile(r'(t\.me/|telegram|телеграм|vk\.com/)', re.I)


def main(src, dst, analysis):
    ads = json.load(open(analysis, encoding='utf-8')).get('ads', {})
    if not ads:
        shutil.copy(src, dst)
        return
    try:
        try:
            import pymupdf as fitz
        except ImportError:
            import fitz
        doc = fitz.open(src)
        touched, n = set(), 0
        for line, pages in ads.items():
            for p in pages:
                page = doc[p - 1]
                rects = page.search_for(line)
                if not rects:  # строка могла разбиться по-другому — ищем по словам со ссылкой
                    for w in line.split():
                        if AD_STRONG.search(w) or w.startswith('http'):
                            rects += page.search_for(w)
                for r in rects:
                    page.add_redact_annot(r, fill=(1, 1, 1))
                    n += 1
                if rects:
                    touched.add(p - 1)
        for i in touched:
            doc[i].apply_redactions(images=0)  # 0 = картинки не трогать
        left = [i + 1 for i in touched if AD_STRONG.search(doc[i].get_text())]
        doc.save(dst, garbage=3, deflate=True)
        print('    Вырезано рекламных фрагментов: %d на стр. %s' % (n, ','.join(str(i + 1) for i in sorted(touched))))
        if left:
            print('    ВНИМАНИЕ: реклама осталась на стр. %s — удали её из исходника вручную' % left)
    except Exception as e:
        shutil.copy(src, dst)
        print('    ВНИМАНИЕ: не удалось вырезать рекламу (%s). Перевод без очистки.' % e)


if __name__ == '__main__':
    if len(sys.argv) != 4:
        sys.exit(__doc__)
    main(*sys.argv[1:])
