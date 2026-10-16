#!/usr/bin/env python3
"""Подготовка EPUB к переводу bbm.

1. Переменные в тексте (<em>θ</em>, <em>n</em>, <em>x</em>...) → <var data-em="1">.
   bbm стирает inline-разметку абзаца; var вместе с sub исключаются из перевода
   (--exclude-translate-tags) и возвращаются на место метками. Термины-курсивы
   (<em>gradient descent</em>) не трогаем — их надо переводить.
2. Подписи <span class="label">Figure 4-1. </span> → <var data-span="1">: остаются
   английскими, а прочие span (keep-together) переводятся вместе с текстом.
   В заголовках номер (<h2><span class="label">A.2 </span>…) уходит в атрибут data-label: из заголовков
   bbm вырезает любую разметку, и без этого модель переделывала «A.2» в «2.2», «A.4» — в «Глава 4.».
3. Ячейки/абзацы из одних чисел (<td><p>0</p></td>) → <var data-num="1">,
   иначе модель пишет «ноль», «двое».
4. Код курсивом (Wiley, OCP 09.10): ключевые слова и идентификаторы набраны <i>switch</i>, а не <code>.
   bbm стирает <i>, и модель переводила их: break → «Перерыв», switch → «переключатель».
   В прозе такой <i> → <var data-i="1"> (возвращается курсивом). В заголовках разметку bbm вырезает целиком,
   поэтому слова записываются в атрибут data-ikw и в карту --keep-map {ключ заголовка: [слова]}:
   адаптер Rosetta передаёт их модели глоссарием «switch -> switch», restore_epub снова делает их курсивом.
5. Буквы вариантов ответа (книги-экзамены): «A, C, D.» в начале абзаца/пункта и «option B», «options D and E»
   в тексте → <var data-opt="1">: модель меняла B на «Б», C на «В», выбрасывала букву ответа.
Обратное преобразование — restore_epub.py. Nav и предметный указатель не трогаем.

Использование:
  python prep_epub.py in.epub out.epub [--keep-map keep.json]
  python prep_epub.py --index-files book.epub   # файлы указателя через запятую (для --exclude_filelist)

Опыт 14.10 (переменная окружения ITALICS=1): курсив в прозе (<em>термин</em>, <i>слово</i> — не переменные и не код)
→ ⟪термин⟫. bbm отдаёт модели только текст, и курсив пропадал (Géron гл. 10: em 69 → 41); с пометкой адаптер
показывает его модели как <em>…</em>, restore_epub возвращает <em>. Вне pre/code/math/заголовков.
"""
import html as htmlmod, json, os, posixpath, re, sys, zipfile

EM = re.compile(r'<em(\s[^>]*)?>([^<]*)</em>')
LABEL = re.compile(r'<span(\s[^>]*\bclass="label"[^>]*)>([^<]*)</span>')
HEAD_LABEL = re.compile(r'<(h[1-6])((?:\s[^>]*)?)>(\s*)<span\s[^>]*\bclass="label"[^>]*>([^<]*)</span>\s*')
NUM_BLOCK = re.compile(r'<(p|td|th)(\s[^>]*)?>(\s*)([^<]*?)(\s*)</\1>')
NUMERIC = re.compile(r'^[\d\s.,:;%±≥≤<>=+\-–—−×/()]+$')
HTML = ('.html', '.xhtml', '.htm')

HEADING = re.compile(r'<(h[1-6])((?:\s[^>]*)?)>(.*?)</\1>', re.S)
ITAL = re.compile(r'<i(\s[^>]*)?>([^<]{1,60})</i>')
HEAD_CODE = re.compile(r'<(i|em|code)\b[^>]*>([^<]{1,60})</\1>')

# Ключевые слова Java, которые в курсиве почти всегда код (в прозе и заголовках)
KW_PROSE = set('''abstract assert boolean break case catch char continue default else enum extends final finally float
implements import instanceof int native null private protected public return short static strictfp switch synchronized
throw throws transient try var void volatile while yield sealed non-sealed permits requires exports opens provides
true false for-each do/while try-with-resources multi-catch javac java jar jdeps jmod jlink jpackage javadoc'''.split())
# В заголовке курсивом выделяют только код, поэтому там годятся и слова, совпадающие с английскими
KW_HEAD = KW_PROSE | set('class interface record module package new this super for if do byte long double '
                         'module open uses to with transitive when'.split())


# Короткие английские слова, которые выделяют курсивом для ударения («are *not* spread», «a *lot* of»):
# это не переменные, их надо переводить (Géron 12.10: «экземпляры not равномерно распределены»).
# Обозначение с индексом (<em>fan</em><sub>in</sub>) остаётся переменной — см. convert().
EMPHASIS = set('''an the not no nor and but or yet so all any few lot own too very is are was were be
    been am do does did can may must has had have how why who what when where at to of by as if it its we
    you our one two new old big bad end try use see way out off now yes get let tie non per via'''.split())


def is_variable(text, indexed=False):
    t = text.strip()
    if not indexed and t.lower() in EMPHASIS:
        return False
    return bool(t) and (len(t) <= 3 or not re.search(r'[A-Za-z]{4}', t))


def code_shape(x):
    """Вид кода сам по себе: вызов(), имя.с.точкой, snake_case, @Аннотация, --флаг, camelCase, StringBuilder.
    «e.g.», «i.e.», «and/or» — не код."""
    return bool(re.search(r'\w\([^()]*\)$', x)                                 # main(), equals(Object)
                or re.search(r'\w\.\w{2,}', x) or re.fullmatch(r'\.\w{2,}', x)  # Files.lines, MANIFEST.MF, .class
                or re.search(r'[a-z]_[a-z]', x, re.I) or re.fullmatch(r'@\w+', x) or re.fullmatch(r'--?\w[\w-]*', x)
                or re.search(r'[a-z][A-Z]', x)                                  # moduleName, camelCase
                or re.fullmatch(r'[A-Z][a-z0-9]+(?:[A-Z][a-z0-9]*)+', x))       # StringBuilder, IOException


def is_code_word(x, heading=False, italic_code=True):
    """Курсив, похожий на код. italic_code — в книге курсивом набран код (Wiley: <i>moduleName</i>, <i>switch</i>):
    тогда кодом считаются и ключевые слова, и одиночные буквы-переменные; иначе — только вид кода (code_shape),
    чтобы «the <i>default</i> value» в обычной книге переводилось."""
    x = x.strip()
    if not x or not re.fullmatch(r'[\w.\-/()\[\]<>@$:]+', x):
        return False
    if code_shape(x):
        return True
    return italic_code and (x in (KW_HEAD if heading else KW_PROSE) or bool(re.fullmatch(r'[A-Za-z]\d?|[A-Z]{2}', x)))


ITALIC_CODE_MIN = 5          # столько <i> вида кода (camelCase, main(), Files.lines) — и книга набирает код курсивом


def italic_code_book(texts):
    n = sum(code_shape(x.strip()) for t in texts for x in re.findall(r'<i(?:\s[^>]*)?>([^<]{1,60})</i>', t))
    return n >= ITALIC_CODE_MIN


INDEX = re.compile(r'<(?:section|body|div)\b[^>]*(?:data-type="index"|epub:type="[^"]*\bindex\b)')


def is_index(text):
    return INDEX.search(text) is not None


def skip(text):
    return '<nav' in text or is_index(text)


def head_key(text):
    """Ключ заголовка, как его видит адаптер: только буквы и цифры в нижнем регистре."""
    return re.sub(r'[\W_]+', '', htmlmod.unescape(re.sub(r'<[^>]+>', '', text))).lower()


# --- буквы вариантов ответа ---------------------------------------------------------------
# между тегом и буквами бывает пустой span-метка страницы (Wiley: <li><span epub:type="pagebreak"/> A, D. …) — 7 ответов OCP
ANSWER = re.compile(r'(<(?:p|li)\b[^>]*>\s*(?:<span\b[^>]*/>\s*|<span\b[^>]*>\s*</span>\s*)*)([A-J](?:, [A-J])*\.)(?=\s)')
# «I» — только если дальше «is/are/and/or» или знак: «the option I chose» — местоимение
LETTER = r'(?:[A-HJ]|I(?=\s+(?:is|are|was|and|or)\b|\s*[,.;:)]))(?![\w-]|[\'’](?!s\b))'
OPTREF = re.compile(r'\b((?:[Oo]ption|[Cc]hoice)s?\s+)(' + LETTER +
                    r'(?:(?:\s*,\s*(?:and\s+|or\s+)?|\s+(?:and|or)\s+)' + LETTER + r')*)')
TOKEN = re.compile(r'(<[^>]+>)')
SKIPTAG = re.compile(r'<(/?)(pre|code|var|math|script|style|kbd|samp)\b[^>]*?(/?)>', re.I)


def map_text(html, fn):
    """fn применяется к текстовым узлам вне pre/code/var/math."""
    parts, depth = TOKEN.split(html), 0
    for i, p in enumerate(parts):
        if p.startswith('<'):
            m = SKIPTAG.match(p)
            if m and not m.group(3):
                depth = max(0, depth + (-1 if m.group(1) else 1))
        elif not depth and p.strip():
            parts[i] = fn(p)
    return ''.join(parts)


def convert(text, stats, keep, italic_code=True):
    def em(m):
        indexed = re.match(r'\s*<su[bp]\b', m.string[m.end():m.end() + 12]) is not None   # fan<sub>in</sub>
        if not is_variable(m.group(2), indexed):
            return m.group(0)
        stats['em→var'] += 1
        return '<var data-em="1"%s>%s</var>' % (m.group(1) or '', m.group(2))

    def num(m):
        tag, attrs, pre, body, post = m.groups()
        if not body or not NUMERIC.match(body) or not re.search(r'\d', body):
            return m.group(0)
        stats['числа'] += 1
        return '<%s%s>%s<var data-num="1">%s</var>%s</%s>' % (tag, attrs or '', pre, body, post, tag)

    def label(m):
        stats['подписи'] += 1
        return '<var data-span="1"%s>%s</var>' % (m.group(1), m.group(2))

    def head(m):
        stats['номера заголовков'] += 1
        return '<%s%s data-label="%s">%s' % (m.group(1), m.group(2), m.group(4).replace('"', '&quot;'), m.group(3))

    def answer(m):
        stats['буквы ответов'] += 1
        return '%s<var data-opt="1">%s</var>' % (m.group(1), m.group(2))

    def optref(t):
        def one(m):
            stats['ссылки на варианты'] += 1
            return m.group(1) + re.sub(r'\b[A-J]\b', lambda l: '<var data-opt="1">%s</var>' % l.group(0), m.group(2))
        return OPTREF.sub(one, t)

    def ital(m):
        if not is_code_word(m.group(2), italic_code=italic_code):
            return m.group(0)
        stats['код курсивом'] += 1
        return '<var data-i="1"%s>%s</var>' % (m.group(1) or '', m.group(2))

    def heading(m):
        words = []
        for t in HEAD_CODE.finditer(m.group(3)):
            w = htmlmod.unescape(t.group(2)).strip()
            if '<' in w or '>' in w:               # List<T>: в системном промпте Rosetta угловые скобки опасны
                continue
            if (t.group(1) == 'code' or is_code_word(w, heading=True, italic_code=italic_code)) \
                    and w not in [x[2:] for x in words]:
                words.append(('c:' if t.group(1) == 'code' else 'i:') + w)
        if not words:
            return m.group(0)
        stats['код в заголовках'] += len(words)
        # второй ключ — без <code>: если bbm отдаст код заголовка меткой ⟦code1⟧, адаптер уберёт её из ключа
        for k in {head_key(m.group(3)), head_key(re.sub(r'<code\b.*?</code>', '', m.group(3), flags=re.S))}:
            keep[k] = sorted(set(keep.get(k, [])) | {w[2:] for w in words})
        attr = htmlmod.escape('|'.join(words), quote=True)
        return '<%s%s data-ikw="%s">%s</%s>' % (m.group(1), m.group(2), attr, m.group(3), m.group(1))

    text = EM.sub(em, text)
    text = HEAD_LABEL.sub(head, text)
    text = HEADING.sub(heading, text)
    # курсив-код и ссылки на варианты — только вне заголовков (там разметку bbm всё равно вырезает)
    pieces = HEADING.split(text)        # [вне, тег, атрибуты, содержимое, вне, …]
    for i in range(0, len(pieces), 4):
        pieces[i] = ITAL.sub(ital, pieces[i])
        pieces[i] = ANSWER.sub(answer, pieces[i])
        pieces[i] = map_text(pieces[i], optref)
    out = []
    for i in range(0, len(pieces), 4):
        out.append(pieces[i])
        if i + 3 < len(pieces):
            out.append('<%s%s>%s</%s>' % (pieces[i + 1], pieces[i + 2] or '', pieces[i + 3], pieces[i + 1]))
    text = ''.join(out)
    text = LABEL.sub(label, text)
    text = NUM_BLOCK.sub(num, text)
    if os.environ.get('ITALICS') == '1':
        text = mark_italics(text, stats)
    return text


ITAL_ANY = re.compile(r'<(em|i)(?:\s[^>]*)?>([^<⟪⟫]{1,200})</\1>')
NO_ITAL = re.compile(r'(<(pre|code|math|script|style|h[1-6])\b.*?</\2>)', re.S)


def mark_italics(text, stats):
    """Курсив в прозе → ⟪…⟫ (переменные и код уже стали <var>). pre/code/math/заголовки не трогаются."""
    parts = NO_ITAL.split(text)              # [вне, блок, тег, вне, блок, тег, …]
    for i in range(0, len(parts), 3):
        def one(m):
            stats['курсив ⟪⟫'] = stats.get('курсив ⟪⟫', 0) + 1
            return '⟪%s⟫' % m.group(2)
        parts[i] = ITAL_ANY.sub(one, parts[i])
    return ''.join(p for k, p in enumerate(parts) if k % 3 != 2)


def opf_dir(z):
    cont = z.read('META-INF/container.xml').decode('utf-8')
    opf = re.search(r'full-path="([^"]+)"', cont).group(1)
    return posixpath.dirname(opf)


def index_files(path):
    z = zipfile.ZipFile(path)
    base = opf_dir(z)
    names = []
    for n in z.namelist():
        if n.endswith(HTML) and is_index(z.read(n).decode('utf-8', 'replace')):
            names.append(posixpath.relpath(n, base) if base else n)
    return names


def main(src, dst, keep_path=None):
    zin, zout = zipfile.ZipFile(src), zipfile.ZipFile(dst, 'w')
    stats = {'em→var': 0, 'числа': 0, 'подписи': 0, 'номера заголовков': 0,
             'код курсивом': 0, 'код в заголовках': 0, 'буквы ответов': 0, 'ссылки на варианты': 0}
    keep = {}
    italic_code = italic_code_book(zin.read(n).decode('utf-8', 'replace') for n in zin.namelist() if n.endswith(HTML))
    for info in zin.infolist():
        data = zin.read(info.filename)
        if info.filename.endswith(HTML):
            text = data.decode('utf-8')
            if re.search(r'data-(em|num|span|opt|i)="1"', text):
                sys.exit('Книга уже подготовлена (есть data-em/data-num): %s' % info.filename)
            if not skip(text):
                data = convert(text, stats, keep, italic_code).encode('utf-8')
        comp = zipfile.ZIP_STORED if info.filename == 'mimetype' else zipfile.ZIP_DEFLATED
        zout.writestr(info, data, compress_type=comp)
    zout.close()
    if keep_path:
        with open(keep_path, 'w', encoding='utf-8') as f:
            json.dump(keep, f, ensure_ascii=False, indent=0)
    print('    Переменные em→var: %d, подписи span.label: %d, номера заголовков: %d, числовые ячейки: %d'
          % (stats['em→var'], stats['подписи'], stats['номера заголовков'], stats['числа']))
    print('    Код курсивом: %d%s, код в заголовках: %d, буквы ответов: %d, ссылки на варианты: %d'
          % (stats['код курсивом'], '' if italic_code else ' (только вид кода — книга не набирает код курсивом)',
             stats['код в заголовках'], stats['буквы ответов'], stats['ссылки на варианты']))
    if stats.get('курсив ⟪⟫'):
        print('    Курсив в прозе помечен для модели (ITALICS=1): %d' % stats['курсив ⟪⟫'])


if __name__ == '__main__':
    argv = sys.argv[1:]
    keep_path = None
    if '--keep-map' in argv:
        i = argv.index('--keep-map')
        keep_path = argv[i + 1]
        del argv[i:i + 2]
    if len(argv) == 2 and argv[0] == '--index-files':
        print(','.join(index_files(argv[1])))
    elif len(argv) == 2:
        main(argv[0], argv[1], keep_path)
    else:
        sys.exit(__doc__)
