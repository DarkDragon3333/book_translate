#!/usr/bin/env python3
"""Обратное преобразование prep_epub.py после перевода:
<var data-em="1"> → <em>, <var data-span="1"> → <span>, <var data-num="1">…</var> → содержимое,
<var data-i="1"> → <i> (код курсивом), <var data-opt="1"> → буква варианта ответа,
data-ikw у заголовка → слова-код в переведённом заголовке снова курсивом/кодом.
Плюс пробелы у кода: bbm не отдаёт модели код в начале и в конце абзаца и при сборке теряет пробел
(«<code>Error</code>означает», «на<code>new Bird()</code>») — пробел возвращается между тегом и русской буквой.
Курсив в прозе (опыт ITALICS=1): ⟪…⟫ → <em>…</em>; непарные ⟪ ⟫ убираются (курсив там потерян моделью).
Использование: python restore_epub.py translated.epub out.epub
"""
import html as htmlmod, re, sys, zipfile

VAR_EM = re.compile(r'<var((?:\s[^>]*?)?)\sdata-em="1"([^>]*)>(.*?)</var>', re.S)
VAR_SPAN = re.compile(r'<var((?:\s[^>]*?)?)\sdata-span="1"([^>]*)>(.*?)</var>', re.S)
VAR_I = re.compile(r'<var((?:\s[^>]*?)?)\sdata-i="1"([^>]*)>(.*?)</var>', re.S)
VAR_OPT = re.compile(r'<var(?:\s[^>]*?)?\sdata-opt="1"[^>]*>(.*?)</var>', re.S)
HEAD_LABEL = re.compile(r'<(h[1-6])((?:\s[^>]*?)?)\sdata-label="([^"]*)"([^>]*)>')
HEAD_KW = re.compile(r'<(h[1-6])((?:\s[^>]*?)?)\sdata-ikw="([^"]*)"([^>]*)>(.*?)</\1>', re.S)
VAR_NUM = re.compile(r'<var(?:\s[^>]*?)?\sdata-num="1"[^>]*>(.*?)</var>', re.S)
HTML = ('.html', '.xhtml', '.htm')

TOKEN = re.compile(r'(<[^>]+>)')
CYR_START = re.compile(r'^[А-Яа-яЁё«]')
CYR_END = re.compile(r'[А-Яа-яЁё]$')
CLOSE = re.compile(r'</(code|a|var|i|b|em|strong|span|kbd|samp)\s*>')
OPEN = re.compile(r'<(code|a|var|i|kbd|samp)\b([^>]*?)(/?)>')


def fix_spaces(text, stats):
    parts, pre = TOKEN.split(text), 0
    for i, p in enumerate(parts):
        if not p.startswith('<'):
            continue
        if re.match(r'<pre\b', p):
            pre += 1
        elif p.startswith('</pre'):
            pre = max(0, pre - 1)
        if pre:
            continue
        m = CLOSE.match(p)
        if m and i + 1 < len(parts) and CYR_START.match(parts[i + 1]):
            prev = parts[i - 1] if i >= 1 else ''
            empty = prev == '' and i >= 2 and re.match(r'<%s\b' % m.group(1), parts[i - 2])
            if not empty:
                parts[i + 1] = ' ' + parts[i + 1]
                stats['пробелы'] += 1
            continue
        m = OPEN.match(p)
        if m and not m.group(3) and 'noteref' not in m.group(2) and i >= 1 and CYR_END.search(parts[i - 1]):
            parts[i - 1] += ' '
            stats['пробелы'] += 1
    return ''.join(parts)


def wrap_words(m, stats):
    tag, a1, words, a2, body = m.groups()
    # длинные слова первыми: this() раньше this, чтобы this не нашлось внутри this()
    items = sorted(htmlmod.unescape(words).split('|'), key=lambda x: -len(x))
    for item in items:
        kind, w = item[:2], item[2:]
        if not w:
            continue
        rx = re.compile(r'(?<![\w-])' + re.escape(htmlmod.escape(w, quote=False)) + r'(?![\w(-])')
        parts = TOKEN.split(body)
        for i, p in enumerate(parts):
            if p.startswith('<') or (i >= 1 and re.match(r'<(i|code)\b', parts[i - 1])):
                continue                           # тег или текст уже обёрнутого слова
            mm = rx.search(p)
            if mm:
                t = 'code' if kind == 'c:' else 'i'
                parts[i] = p[:mm.start()] + '<%s>%s</%s>' % (t, mm.group(0), t) + p[mm.end():]
                stats['код в заголовках'] += 1
                break
        body = ''.join(parts)
    return '<%s%s%s>%s</%s>' % (tag, a1, a2, body, tag)


def restore(text, stats):
    def em(m):
        stats['em'] += 1
        return '<em%s%s>%s</em>' % (m.group(1), m.group(2), m.group(3))
    text = VAR_EM.sub(em, text)

    def ital(m):
        stats['i'] += 1
        return '<i%s%s>%s</i>' % (m.group(1), m.group(2), m.group(3))
    text = VAR_I.sub(ital, text)
    def opt(m):
        # буква в начале ответа — край абзаца, bbm теряет за ней пробел («J.Код…»)
        stats['opt'] += 1
        before, after = m.string[m.start() - 1:m.start()], m.string[m.end():m.end() + 1]
        return ((' ' if re.match(r'[А-Яа-яЁё]', before) else '') + m.group(1)
                + (' ' if re.match(r'[А-Яа-яЁёA-Za-z«(]', after) else ''))
    text = VAR_OPT.sub(opt, text)

    def span(m):
        stats['span'] += 1
        return '<span%s%s>%s</span>' % (m.group(1), m.group(2), m.group(3))
    text = VAR_SPAN.sub(span, text)
    text, n = VAR_NUM.subn(r'\1', text)
    stats['num'] += n
    text = HEAD_KW.sub(lambda m: wrap_words(m, stats), text)

    def head(m):
        stats['head'] += 1
        return '<%s%s%s><span class="label">%s</span>' % (m.group(1), m.group(2), m.group(4), m.group(3).replace('&quot;', '"'))
    text = HEAD_LABEL.sub(head, text)
    if '⟪' in text or '⟫' in text:
        text = restore_italics(text, stats)
    return fix_spaces(text, stats)


def balanced(s):
    stack = []
    for m in re.finditer(r'<(/?)([a-zA-Z][\w:-]*)[^>]*?(/?)>', s):
        if m.group(3):
            continue
        if not m.group(1):
            stack.append(m.group(2))
        elif not stack or stack.pop() != m.group(2):
            return False
    return not stack


def restore_italics(text, stats):
    def one(m):
        if '<' in m.group(1) and not balanced(m.group(1)):
            return m.group(1)
        stats['курсив'] += 1
        return '<em>%s</em>' % m.group(1)
    text = re.sub(r'⟪([^⟪⟫]{1,400}?)⟫', one, text)
    n = text.count('⟪') + text.count('⟫')
    stats['курсив без пары'] += n
    return text.replace('⟪', '').replace('⟫', '')


def main(src, dst):
    zin, zout = zipfile.ZipFile(src), zipfile.ZipFile(dst, 'w')
    stats = {'em': 0, 'num': 0, 'span': 0, 'head': 0, 'i': 0, 'opt': 0, 'код в заголовках': 0, 'пробелы': 0,
             'курсив': 0, 'курсив без пары': 0}
    for info in zin.infolist():
        data = zin.read(info.filename)
        if info.filename.endswith(HTML) and '<nav' not in data.decode('utf-8', 'replace'):
            data = restore(data.decode('utf-8'), stats).encode('utf-8')
        comp = zipfile.ZIP_STORED if info.filename == 'mimetype' else zipfile.ZIP_DEFLATED
        zout.writestr(info, data, compress_type=comp)
    zout.close()
    print('    Возвращено: курсив-переменные %d, подписи %d, номера заголовков %d, числовые ячейки %d'
          % (stats['em'], stats['span'], stats['head'], stats['num']))
    print('    Код курсивом %d, код в заголовках %d, буквы вариантов %d, пробелы у кода %d'
          % (stats['i'], stats['код в заголовках'], stats['opt'], stats['пробелы']))
    if stats['курсив'] or stats['курсив без пары']:
        print('    Курсив в прозе: возвращён %d, непарных пометок убрано %d' % (stats['курсив'], stats['курсив без пары']))


if __name__ == '__main__':
    if len(sys.argv) != 3:
        sys.exit(__doc__)
    main(sys.argv[1], sys.argv[2])
