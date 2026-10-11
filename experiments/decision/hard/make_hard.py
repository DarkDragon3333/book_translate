r"""Мини-книга для короткой проверки: трудные абзацы Spring in Action в одной главе.
Берёт английский EPUB, находит абзацы из hard_sel.json (28 известных ошибок старого перевода, 20 верных
для контроля, 20 самых насыщенных новыми терминами) и кладёт их вместо содержимого ch028.xhtml.
Перевод потом: -e ONLY=ch028.xhtml translate Spring_hard.epub programming,distributed (~5 мин).
Запуск (из C:\book_translate):
  docker.exe run --rm -v C:\book_translate\experiments\decision\hard:/w -v C:\book_translate\books:/books --entrypoint python book_translate/pdf-epub:1 /w/make_hard.py "/books/Spring.in.Action_epub/Spring.in.Action.epub" /books/Spring_hard.epub
"""
import json, os, re, sys, zipfile
from html import unescape

TAGS = ['p', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'li', 'dt', 'dd', 'td', 'th', 'figcaption', 'caption']
PH = re.compile(r'⟦[^⟧]*⟧')


def norm(s):
    s = re.sub(r'<br\b[^>]*>', ' ', s)                 # строчные теги убираем без пробела: <em>bean</em>s → beans
    return re.sub(r'\s+', ' ', unescape(re.sub(r'<[^>]+>', '', s))).strip()


def pattern(src):
    """Текст абзаца из журнала (код заменён метками ⟦…⟧) → регулярка по тексту элемента."""
    parts = [re.escape(re.sub(r'\s+', ' ', p).strip()) for p in PH.split(src)]
    rx = r'\s*.*?\s*'.join(p for p in parts if p)
    if src.strip().startswith('⟦'):
        rx = '.*?' + rx
    if src.strip().endswith('⟧'):
        rx += '.*?'
    return re.compile(rx, re.S)


def main():
    src_epub, out_epub = sys.argv[1], sys.argv[2]
    sel = json.load(open(os.path.join(os.path.dirname(os.path.abspath(__file__)), 'hard_sel.json'), encoding='utf-8'))
    z = zipfile.ZipFile(src_epub)
    names = [n for n in z.namelist() if n.endswith(('.xhtml', '.html')) and not n.endswith('nav.xhtml')]
    cands = []                                   # (tag, outer html, текст) — сначала p, потом остальные
    for tag in TAGS:
        for n in sorted(names):
            for m in re.finditer(r'<%s\b[^>]*>.*?</%s>' % (tag, tag), z.read(n).decode('utf-8', 'ignore'), re.S):
                cands.append((tag, m.group(0), norm(m.group(0))))
    picked, missing = [], []
    for kind in ('err', 'ctrl', 'dense'):
        for line in sel['sel'][kind]:
            rx = pattern(sel['src'][str(line)])
            hit = next((c for c in cands if rx.fullmatch(c[2])), None)
            (picked.append((line, hit)) if hit else missing.append(line))
    target = next(n for n in names if n.endswith('ch028.xhtml'))
    page = z.read(target).decode('utf-8')
    body = []
    for line, (tag, html, _) in picked:
        if tag == 'li':
            html = '<ul>' + html + '</ul>'
        elif tag in ('td', 'th', 'dt', 'dd', 'caption', 'figcaption'):
            html = re.sub(r'^<%s\b[^>]*>(.*)</%s>$' % (tag, tag), r'<p>\1</p>', html, flags=re.S)
        body.append(html)
    page = re.sub(r'(<body\b[^>]*>).*(</body>)', lambda m: m.group(1) + '\n' + '\n'.join(body) + '\n' + m.group(2), page, flags=re.S)
    with zipfile.ZipFile(out_epub, 'w') as o:
        for item in z.infolist():
            data = page.encode('utf-8') if item.filename == target else z.read(item.filename)
            o.writestr(item, data, compress_type=zipfile.ZIP_STORED if item.filename == 'mimetype' else zipfile.ZIP_DEFLATED)
    print('Абзацев в мини-главе: %d из %d (не найдено: %s)' % (len(picked), len(picked) + len(missing), missing or 'нет'))
    print('Файл: %s' % out_epub)


if __name__ == '__main__':
    main()
