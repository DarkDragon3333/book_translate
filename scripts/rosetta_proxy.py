#!/usr/bin/env python3
"""Прокси-адаптер между bbm и моделью-переводчиком Rosetta (родной формат модели вместо формата bbm).
(Ветки Hy-MT2 и Index-Translate — опыты 12.10, модели не взяты — удалены 14.10; они есть в истории git.)

bbm шлёт: system="" и user = [<glossary>…</glossary> + правила про метки +] «Please help me to translate,`текст`…»,
ждёт JSON {"ru_translation": "…"}. Rosetta обучена на другом формате:
  system: «Translate the user's text to Russian.» + Context/Tone + «Glossary:» «- term -> перевод» + «Output format: JSON»
  user:   JSON с исходным текстом, ответ — такой же JSON с переводом.
Прокси:
- отвечает на пробы bbm сам (PONG, schema_ok);
- вынимает из запроса bbm текст абзаца и термины глоссария, собирает запрос в формате Rosetta;
- из ответа берёт перевод (JSON {"text": …}; если JSON битый — текст как есть);
- чинит метки: имя восстанавливается по номеру из исходника (⟦кода5⟧ → ⟦code5⟧, ⟦var7⟧ остаётся ⟦var7⟧);
- если метки не сошлись, ответ пустой или не JSON — повтор с temperature 0 (метки перечисляются явно),
  если метки всё ещё не сошлись — ещё один повтор (иначе bbm дописывает потерянный код в конец абзаца);
- слова-код из заголовков (карта --keep) передаёт модели глоссарием «switch -> switch»;
- отдаёт bbm {"ru_translation": …}, пишет журнал (исходник, термины, сырой ответ, итог, метки, повтор).

Запуск внутри контейнера translate:
  python rosetta_proxy.py [--listen 127.0.0.1:8083] [--target http://ollama:11434] [--log /books/eval/rosetta_log.jsonl]
                          [--context "A technical book about software development."] [--domains ml,math] [--logprobs]
--domains: Context строится из тем глоссариев книги («A technical book about machine learning and mathematics.»)
вместо общего --context (тест 13.10: переводчику говорили «software development» и для книги по ML).
--logprobs: в журнал пишется уверенность модели в каждом токене ответа (поле lp) — для проверки детектора ошибок.
С 13.10 (разбор ошибок 4B в заголовках и абзацах):
- латинские сокращения без подсказки глоссария (HGB, LOF, MCD…) получают подсказку «X -> X»: иначе модель придумывает
  русские (ГБГ, ФЛВ, МКО; в Géron целиком ~250: МЛП, НС, ГК…); заголовок из одного сокращения или названия
  (MNIST, LLE, BIRCH, AdaBoost) модели не отправляется — остаётся как в оригинале («МИНИТС», «ЛЛЕ», «БЕРЁЗА»);
- термин с заглавными буквами совпадает и в заголовке, где каждое слово с заглавной («The ROC Curve» — «ROC curve»);
- термин с буквой-переменной (k-means) находится и тогда, когда курсивная k стала меткой: «⟦var35⟧-means» →
  подсказка «⟦var35⟧-means -> метод ⟦var35⟧-средних» (--glossary-file).
С 14.10:
- невидимые символы (U+2060 WORD JOINER, U+200B, U+00AD, U+FEFF…) модели не отправляются, перед метками возвращаются:
  O'Reilly ставит U+2060 перед номером сноски, и модель теряла такую метку в 27% случаев (25 из 94; остальные метки ~1%);
- курсив в прозе (опыт, epub.sh -e ITALICS=1): prep_epub помечает его ⟪…⟫, модели он уходит как <em>…</em>,
  обратно — снова ⟪…⟫ (restore_epub делает <em>); непарные пометки убираются, курсив тогда теряется, как раньше.
Гибридный режим (14.10, epub.sh MODEL=hybrid), проход 3: --replay <журнал быстрого прохода> --pick <.hybrid_pick.json>.
  Абзац не из списка отбора — ответ из журнала быстрой модели сразу, без модели (в журнал: "hybrid": "fast");
  из списка (или которого нет в журнале) — переводит модель запроса, сильная (rosetta12-ru): "hybrid": "strong",
  строка в --done (по ним интерфейс считает ход прохода).
"""
import argparse, http.server, json, re, threading, time, unicodedata, urllib.request

a = argparse.ArgumentParser()
a.add_argument('--listen', default='127.0.0.1:8083')
a.add_argument('--target', default='http://ollama:11434')
a.add_argument('--log', default='/books/eval/rosetta_log.jsonl')
a.add_argument('--context', default='A technical book about software development.')
a.add_argument('--tone', default='Clear, natural technical prose, as in a professionally edited book.')
a.add_argument('--senses', help='карта смыслов книги (.senses.json от scripts/senses.py)')
a.add_argument('--senses-rules', default='/config/senses.csv')
a.add_argument('--keep', help='карта кода в заголовках {ключ заголовка: [слова]} от prep_epub.py --keep-map')
a.add_argument('--domains', help='темы глоссариев книги (ml,math): Context строится из них, --context не используется')
a.add_argument('--logprobs', action='store_true', help='писать в журнал уверенность модели по токенам (Rosetta)')
a.add_argument('--glossary-file', help='глоссарий книги для bbm (.glossary.txt): термины с буквой-переменной (k-means)')
a.add_argument('--replay', help='гибрид: журнал адаптера быстрого прохода (.hybrid_fast.jsonl)')
a.add_argument('--pick', help='гибрид: список абзацев для сильной модели (.hybrid_pick.json от hybrid_pick.py)')
a.add_argument('--done', help='гибрид: файл, куда дописывается строка за каждый абзац сильной модели')
args = a.parse_args()

# Тема книги для поля Context — по глоссариям, с которыми её переводят (base — общий, keep — служебный)
TOPICS = {'programming': 'programming', 'web': 'web development', 'ml': 'machine learning', 'math': 'mathematics',
          'devops': 'DevOps and cloud infrastructure', 'distributed': 'distributed systems',
          'security': 'information security', 'gamedev': 'game development', 'lowlevel': 'low-level systems programming'}


def topic_context(domains):
    names = []
    for d in (domains or '').split(','):
        t = TOPICS.get(d.strip())
        if t and t not in names:
            names.append(t)
    if not names:
        return None
    return 'A technical book about %s.' % (names[0] if len(names) == 1 else ', '.join(names[:-1]) + ' and ' + names[-1])


if args.domains and topic_context(args.domains):
    args.context = topic_context(args.domains)
lock = threading.Lock()
opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

# Гибрид: ответы быстрого прохода по тексту абзаца (как его прислал bbm) и список абзацев для сильной модели
REPLAY, PICK = None, set()
if args.replay:
    REPLAY = {}
    for _l in open(args.replay, encoding='utf-8'):
        if _l.strip():
            _r = json.loads(_l)
            REPLAY.setdefault(_r['src'], _r)
    if args.pick:
        PICK = set(json.load(open(args.pick, encoding='utf-8'))['picked'])
    print('rosetta_proxy: гибрид — ответов быстрого прохода %d, для сильной модели %d' % (len(REPLAY), len(PICK)), flush=True)
HYB_DONE = [0]
if args.done:
    try:
        HYB_DONE[0] = sum(1 for _ in open(args.done, encoding='utf-8'))
    except OSError:
        pass

ASK = re.compile(r'Please help me to translate,`(.*)` to Russian, please return only translated content', re.S)
GLOSS = re.compile(r'<glossary>\n(.*?)\n</glossary>', re.S)
MARK = re.compile(r'⟦([a-z]+)(\d+)⟧')
# метка, которую модель испортила. Видели: ⟦ кода 5 ⟧, ⟦code5' (апостроф из &#x27;), ⟦code48], ⟦code136;], ⟦code227;⟧,
# ⟦var135» (модель закрыла метку кавычкой «…»), ⟦в степени 83⟧ (имя метки переведено).
# Имя — до 3 слов, номер обязателен; закрывающая: ⟧ | ] | » (кавычка возвращается в текст) | апостроф.
ANY_MARK = re.compile(r'⟦\s*((?:[^\W\d_]+\s+){0,2}[^\W\d_]*)\s*(\d+)\s*;?\s*(⟧|\]|»|[\'’](?!⟧))')


# Текст вида <s>, </s>, <i> в абзаце (не код, а обозначения в прозе): на «</s>» Rosetta (Gemma 3) обрывает ответ.
# Модели такие теги уходят как &lt;s&gt; и возвращаются обратно. Тест 08.10 (test_tags.py, 2 абзаца Géron × 2 прогона):
# как есть — обрыв 4/4; ‹s› — теги потеряны 2/4; &lt;s&gt; — целиком и с тегами 4/4.
TAG = re.compile(r'</?[A-Za-z_][\w:-]*>')


def esc_tags(src):
    if '&lt;' in src or '&gt;' in src:           # в тексте уже есть сущности — не трогаем, обратно не отличить
        return src
    return TAG.sub(lambda m: '&lt;' + m.group(0)[1:-1] + '&gt;', src)


def unesc_tags(src, out):
    names = {t[1:-1] for t in TAG.findall(src)}
    if not names or '&lt;' in src or '&gt;' in src:
        return out
    return re.sub(r'&lt;(/?[A-Za-z_][\w:-]*)&gt;', lambda m: '<%s>' % m.group(1) if m.group(1) in names else m.group(0), out)


KEEP = {}
if args.keep:
    try:
        KEEP = json.load(open(args.keep, encoding='utf-8'))
    except Exception as e:
        print('rosetta_proxy: карта кода в заголовках не загружена: %s' % e, flush=True)


def head_key(text):
    """Тот же ключ, что в prep_epub.head_key: только буквы и цифры, нижний регистр, без меток."""
    return re.sub(r'[\W_]+', '', MARK.sub('', text)).lower()


def marks(t):
    return sorted(m.group(0) for m in MARK.finditer(t))


def fix_marks(src, out):
    names = {m.group(2): m.group(1) for m in MARK.finditer(src)}
    out = re.sub(r'(⟦[^⟧\s]{0,20}\d+)\\(?:x|u00|u)27;?', r"\1'", out)   # ⟦code783\u27; — апостроф, экранированный моделью

    def sub(m):
        name = names.get(m.group(2))
        if name is None and not re.fullmatch(r'[a-z]*', m.group(1).strip()):
            return m.group(0)                    # номера нет в исходнике и имя не латиница — не трогаем
        return '⟦%s%s⟧' % (name or m.group(1).strip() or 'code', m.group(2)) + ('»' if m.group(3) == '»' else '')
    return ANY_MARK.sub(sub, out)


# Смысл многозначных терминов (feature, transformer, recall…): карта строится до перевода по всей книге
# (scripts/senses.py), здесь по абзацу берётся решение: свой перевод / перевод другого смысла / не подсказывать.
SMAP, SRULES, SDEF = {}, {}, {}
if args.senses:
    try:
        import os, sys
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        import senses as _senses
        SMAP = json.load(open(args.senses, encoding='utf-8'))
        _rules = _senses.load_senses(args.senses_rules)
        SRULES = {w: r['forms'] for w, r in _rules.items()}
        SDEF = {w: r['default'] for w, r in _rules.items()}
    except Exception as e:                       # без карты адаптер работает как раньше
        print('rosetta_proxy: карта смыслов не загружена: %s' % e, flush=True)
        SMAP, SRULES = {}, {}


def apply_senses(src, terms):
    """Возвращает (термины, изменения, слова без решения). Изменения: [слово, 'alt'|'none', перевод]."""
    if not SRULES:
        return terms, [], []
    entry = _senses.lookup(SMAP, _senses.key(src))
    changes, missing, out = [], [], []
    for t in terms:
        w = next((w for w, rx in SRULES.items() if rx.fullmatch(t[0])), None)
        if w is None:
            out.append(t)
            continue
        if entry is None or w not in entry:
            missing.append(w)
            if SDEF.get(w) == 'none':             # слово решают только подсказки в предложении: без решения — не подсказывать
                changes.append([w, 'none', None])
            else:
                out.append(t)
            continue
        kind, tgt = entry[w]
        if kind == 'own':
            out.append(t)
        elif kind == 'alt' and tgt:
            out.append((t[0], tgt))
            changes.append([w, 'alt', tgt])
        else:
            changes.append([w, 'none', None])
    return out, changes, missing


def case_ok(term, src):
    """Термин без заглавных букв совпадает в любом регистре, с заглавными — только точно (целым словом),
    с заглавной первой буквой в начале предложения/заголовка («Inclusive OR» для «inclusive OR») или в заголовке,
    где каждое слово с заглавной («The ROC Curve» для «ROC curve»): строчные слова термина могут начинаться
    с заглавной, слова с заглавными должны совпасть точно («Windows» не сработает на «context windows»)."""
    if term == term.lower():
        return True
    forms = {term, term[0].upper() + term[1:]}
    if any(re.search(r'(?<!\w)' + re.escape(t) + r'(?!\w)', src) for t in forms):
        return True
    parts = re.split(r'(\W+)', term)
    rx = ''.join('[%s%s]%s' % (w[0], w[0].upper(), re.escape(w[1:])) if w and w == w.lower() and w[0].isalpha()
                 else re.escape(w) for w in parts)
    return re.search(r'(?<!\w)' + rx + r'(?!\w)', src) is not None


# Латинские сокращения (2+ заглавные, можно с цифрами): без подсказки модель придумывает русские.
# Не сокращения — слова, которые пишут заглавными для выделения или в служебных заголовках.
ACRO = re.compile(r'(?<![A-Za-z0-9])[A-Z][A-Z0-9]*[A-Z][A-Z0-9]*(?![A-Za-z0-9])')
CAPS_WORDS = set("""NOT NO NEVER ALWAYS ONLY ALL AND OR THE IS ARE BE DO DON DOES ISN AREN CAN CANNOT WILL MAY MUST SHOULD
IF THEN ELSE YES OK NEW OLD ANY EACH EVERY NONE BOTH VERY MORE LESS MOST FIRST LAST ONE TWO HOW WHY WHAT WHEN WHERE
WHO THIS THAT IT TO OF IN ON AT BY FOR WITH WITHOUT NOW STOP WAIT READ WRITE AS AN UP OUT OFF WE YOU HE SHE THEY
NOTE NOTES TIP TIPS WARNING CAUTION IMPORTANT SUMMARY EXERCISE EXERCISES INTRODUCTION CONCLUSION CHAPTER PART
APPENDIX CONTENTS INDEX PREFACE FOREWORD REFERENCES BIBLIOGRAPHY GLOSSARY EXAMPLE EXAMPLES DEFINITION THEOREM
LEMMA PROOF SOLUTION SOLUTIONS QUESTIONS ANSWERS OVERVIEW BACKGROUND SETUP RESULTS DISCUSSION ABSTRACT HINT TODO
FIGURE TABLE LISTING SECTION STEP DONE ERROR INFO DEBUG TRUE FALSE NULL ABOUT USING SEE ALSO KEY TERMS RECAP QUIZ
LAB GOAL GOALS TASK TASKS INPUT OUTPUT CODE DATA TEST TESTS RUN BUILD START END NEXT BACK HOME HELP MENU
AI ML DL OS RAM ROM PC IT USA US UK EU CEO CTO CFO COO PHD FAQ HR KB MB GB TB PB KIB MIB GIB TIB""".split())
# вторая строка: у них есть устоявшиеся русские формы (ИИ, МО, ОС, ОЗУ, ПК, США, ГБ…) или должности — модель переводит их сама


def caps_text(text):
    """Текст набран заглавными (служебный заголовок «ENABLING LIVENESS…»): сокращения в нём не отличить."""
    letters = [c for c in text if c.isalpha()]
    return len(letters) >= 8 and sum(c.isupper() for c in letters) > 0.7 * len(letters)


def auto_keep(src, terms):
    """Сокращения из абзаца, которых нет в подсказках: «HGB -> HGB»."""
    text = MARK.sub(' ', src)
    if caps_text(text):
        return []
    have = ' '.join(t[0] for t in terms)
    out = []
    for w in ACRO.findall(text):
        if w in CAPS_WORDS or w in out or re.search(r'(?<![A-Za-z0-9])%s(?![A-Za-z0-9])' % re.escape(w), have, re.I):
            continue
        out.append(w)
    return out[:8]


NAME = re.compile(r'[A-Z][A-Za-z0-9]*[A-Z][A-Za-z0-9]*')


def name_only(src):
    """Заголовок из одного сокращения или названия (MNIST, [LSI], AdaBoost, DBSCAN) — переводить нечего."""
    t = MARK.sub(' ', src).strip()
    if MARK.search(src) or len(t) > 30:
        return False
    w = t.strip('[]()«»⟪⟫"\' .:')
    return bool(NAME.fullmatch(w)) and w.upper() not in CAPS_WORDS and not (w.isupper() and len(w) > 6)


# Термины вида «k-means», «k-fold cross-validation», «t-SNE»: курсивная буква становится меткой ⟦varN⟧,
# и bbm термин уже не находит. Берём их из глоссария книги и ищем с меткой на месте буквы.
VAR_TERMS = []
if args.glossary_file:
    try:
        for line in open(args.glossary_file, encoding='utf-8'):
            if ' → ' not in line:
                continue
            s_, t_ = (x.strip() for x in line.split(' → ', 1))
            m_ = re.fullmatch(r'([a-z])-(\w[\w -]*)', s_)
            if m_ and re.search(r'(?<![A-Za-zА-Яа-яЁё])%s(?![A-Za-zА-Яа-яЁё])' % m_.group(1), t_):
                VAR_TERMS.append((m_.group(1), re.compile(r'(⟦var\d+⟧)-%s(?!\w)' % re.escape(m_.group(2)), re.I), m_.group(2), t_))
    except Exception as e:
        print('rosetta_proxy: глоссарий для терминов с переменной не загружен: %s' % e, flush=True)


def var_terms(src):
    out = []
    for letter, rx, rest, tgt in VAR_TERMS:
        for m in rx.finditer(src):
            mark = m.group(1)
            hint = (mark + '-' + rest, re.sub(r'(?<![A-Za-zА-Яа-яЁё])%s(?![A-Za-zА-Яа-яЁё])' % letter, mark, tgt, count=1))
            if hint not in out:
                out.append(hint)
    return out


def has_word(text, w):
    return re.search(r'(?<![\w-])' + re.escape(w) + r'(?![\w(-])', text) is not None


def unquote_keep(out, keep):
    """`switch`, 'for' → switch, for: кавычки и обратные апострофы вокруг слов-кода из заголовка
    (курсив вернёт restore_epub)."""
    for w in keep:
        out = re.sub(r'([`\'"])(%s)\1' % re.escape(w), r'\2', out)
    return out


def is_invisible(c):
    return unicodedata.category(c) == 'Cf'      # U+2060, U+200B–U+200D, U+00AD, U+FEFF, знаки направления текста


def inv_pack(src):
    """Текст без невидимых символов и {метка: символы перед ней}."""
    if not any(is_invisible(c) for c in src):
        return src, {}
    pre = {}
    for m in MARK.finditer(src):
        i = m.start()
        while i > 0 and is_invisible(src[i - 1]):
            i -= 1
        if i < m.start():
            pre[m.group(0)] = src[i:m.start()]
    return ''.join(c for c in src if not is_invisible(c)), pre


def inv_unpack(out, pre):
    for mark, chars in pre.items():
        i = out.find(mark)
        if i >= 0 and not out[max(0, i - len(chars)):i] == chars:
            out = out[:i] + chars + out[i:]
    return out


# Курсив в прозе (опыт ITALICS): ⟪…⟫ от prep_epub ↔ <em>…</em> для модели
ITAL = re.compile(r'⟪([^⟪⟫]*)⟫')


def ital_to_model(text):
    return ITAL.sub(r'<em>\1</em>', text)


def ital_from_model(out, wanted):
    out = re.sub(r'&lt;(/?)em&gt;', r'<\1em>', out)
    if not wanted:                               # курсива в исходнике не было — не добавляем
        return re.sub(r'</?em>', '', out)
    out = re.sub(r'<em>([^<⟪⟫]*?)</em>', r'⟪\1⟫', out)
    return re.sub(r'</?em>', '', out)


def system_prompt(terms, has_marks, need=None, lost=None, ital=False):
    s = ["Translate the user's text to Russian.", 'Context: ' + args.context, 'Tone: ' + args.tone]
    if terms:
        s.append('Glossary:')
        s += ['- %s -> %s' % t for t in terms]
    if has_marks:
        s.append('Keep placeholders like ⟦code1⟧ and ⟦var2⟧ exactly as they are, at the right place.')
    if need:
        s.append('The translation must contain each of these placeholders exactly once: %s.' % ', '.join(need))
    if lost:
        s.append('Keep these words in English exactly as written (they are code): %s.' % ', '.join(lost))
    if ital:
        s.append('Keep the <em>…</em> tags around the translation of the same words.')
    s += ['Output format: JSON', 'Provide the final translation immediately without any other text.']
    return '\n'.join(s)


def parse(raw):
    raw = raw.strip()
    m = re.search(r'\{.*\}', raw, re.S)
    if m:
        try:
            v = json.loads(m.group(0)).get('text')
            if isinstance(v, str):
                return v.strip(), True
        except ValueError:
            pass
        # битый JSON (например, \x27 — такого экранирования в JSON нет): значение "text" вынимается вручную,
        # иначе в книгу попадал весь ответ с хвостом «"}»
        t = re.search(r'"text"\s*:\s*"(.*)"\s*\}\s*$', m.group(0), re.S)
        if t:
            v = re.sub(r'\\x27;?', "'", t.group(1))
            try:
                v = json.loads('"%s"' % v)
            except ValueError:
                v = v.replace('\\n', '\n').replace('\\"', '"').replace('\\\\', '\\')
            return tidy(v), False
    # ответ оборван (Rosetta останавливается на «</s>» в тексте): снять начало {"text": ", иначе оно уходит в книгу
    t = re.match(r'\{\s*"text"\s*:\s*"(.*)$', raw, re.S)
    if t:
        return tidy(t.group(1).replace('\\n', '\n').replace('\\"', '"')), False
    return raw, False


def tidy(v):
    """Хвосты битого JSON: «Построение циклов для"», «Коллекторы ти",», «:"Пропуск данных»."""
    v = re.sub(r'^\s*:\s*"', '', v.strip())
    v = re.sub(r'"\s*[,}]*\s*$', '', v)
    return v.strip()


def call(model, system, text, temperature):
    msgs = [{'role': 'system', 'content': system},
            {'role': 'user', 'content': json.dumps({'text': text}, ensure_ascii=False)}]
    q = {'model': model, 'stream': False, 'options': {'temperature': temperature}, 'messages': msgs}
    lp = args.logprobs and model.startswith('rosetta')
    if lp:
        q['logprobs'], q['top_logprobs'] = True, 2   # вероятности до выборки (llama-server n_probs), от температуры не зависят
    body = json.dumps(q).encode()
    req = urllib.request.Request(args.target + '/api/chat', data=body, method='POST',
                                 headers={'Content-Type': 'application/json'})
    with opener.open(req, timeout=900) as r:
        d = json.loads(r.read())
    return d['message']['content'], (lp_pack(d.get('logprobs')) if lp else None)


def lp_pack(lps):
    """Сжатый список токенов ответа: [токен, logprob, лучший другой токен, его logprob] (два последних — только
    у неуверенных токенов, p < 0,5)."""
    out = []
    for t in lps or []:
        row = [t.get('token', ''), round(t.get('logprob', 0.0), 3)]
        if row[1] < -0.69:
            alt = [x for x in t.get('top_logprobs') or [] if x.get('token') != row[0]]
            if alt:
                row += [alt[0].get('token', ''), round(alt[0].get('logprob', 0.0), 3)]
        out.append(row)
    return out


def reply(content, model):
    return {'id': 'chatcmpl-rosetta', 'object': 'chat.completion', 'created': int(time.time()), 'model': model,
            'choices': [{'index': 0, 'message': {'role': 'assistant', 'content': content}, 'finish_reason': 'stop'}],
            'usage': {'prompt_tokens': 0, 'completion_tokens': 0, 'total_tokens': 0}}


class H(http.server.BaseHTTPRequestHandler):
    def _send(self, code, data, ctype='application/json'):
        self.send_response(code)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _forward(self, body=None):
        req = urllib.request.Request(args.target + self.path, data=body, method=self.command,
                                     headers={k: v for k, v in self.headers.items()
                                              if k.lower() not in ('host', 'content-length', 'accept-encoding')})
        try:
            with opener.open(req, timeout=900) as r:
                self._send(r.status, r.read(), r.headers.get('Content-Type', 'application/json'))
        except urllib.error.HTTPError as e:
            self._send(e.code, e.read())

    def do_GET(self):
        self._forward()

    def do_POST(self):
        body = self.rfile.read(int(self.headers.get('Content-Length', 0)))
        if not self.path.startswith('/v1/chat/completions'):
            return self._forward(body)
        q = json.loads(body)
        model = q.get('model', '')
        user = [m['content'] for m in q.get('messages', []) if m.get('role') == 'user']
        last = user[-1] if user else ''
        fmt = json.dumps(q.get('response_format') or {})
        if last.startswith('Reply with the single word: PONG'):
            return self._send(200, json.dumps(reply('PONG.', model)).encode())
        if 'structured_output_probe' in fmt:
            return self._send(200, json.dumps(reply('{"probe": "schema_ok"}', model)).encode())
        m = ASK.search(last)
        if not m:
            return self._forward(body)
        src = m.group(1)
        if REPLAY is not None and src in REPLAY and src not in PICK:     # гибрид: перевод быстрого прохода
            r = dict(REPLAY[src], hybrid='fast')
            content = json.dumps({'ru_translation': r['out']}, ensure_ascii=False) if 'ru_translation' in fmt else r['out']
            self._send(200, json.dumps(reply(content, model), ensure_ascii=False).encode())
            with lock, open(args.log, 'a', encoding='utf-8') as f:
                f.write(json.dumps(r, ensure_ascii=False) + '\n')
            return
        g = GLOSS.search(last)
        terms = [tuple(x.split(' → ', 1)) for x in g.group(1).split('\n') if ' → ' in x] if g else []
        # bbm ищет термины без учёта регистра: «Windows» сработал бы на «context windows», «Spark» — на «spark».
        # Термин с заглавными буквами (название) передаём модели, только если он есть в тексте в точности так же.
        dropped = [t for t in terms if not case_ok(t[0], src)]
        terms = [t for t in terms if case_ok(t[0], src)]
        terms, senses_changed, senses_missing = apply_senses(src, terms)
        keep = [w for w in KEEP.get(head_key(src), []) if w not in {t[0] for t in terms}]
        terms = terms + [(w, w) for w in keep]
        vterms = var_terms(src)
        terms = terms + vterms
        auto = auto_keep(src, terms)
        terms = terms + [(w, w) for w in auto]
        if name_only(src):                         # MNIST, BIRCH, AdaBoost: модели не отправляем
            content = json.dumps({'ru_translation': src}, ensure_ascii=False) if 'ru_translation' in fmt else src
            self._send(200, json.dumps(reply(content, model), ensure_ascii=False).encode())
            with lock, open(args.log, 'a', encoding='utf-8') as f:
                f.write(json.dumps({'src': src, 'terms': terms, 'dropped': dropped, 'keep': keep, 'senses': senses_changed,
                                    'senses_missing': senses_missing, 'out': src, 'marks_ok': True, 'json': True,
                                    'keep_ok': True, 'retries': 0, 'raw': [], 'as_is': True}, ensure_ascii=False) + '\n')
            return
        has_marks = bool(MARK.search(src))
        clean, inv = inv_pack(src)
        n_ital = len(ITAL.findall(src))
        send = ital_to_model(esc_tags(clean))
        temps = (q.get('temperature', 0.2), 0.0, 0.4)
        tries = []
        for n, temp in enumerate(temps):
            if n == 2 and tries[-1]['marks_ok']:
                break                              # третья попытка — только ради меток
            need = marks(src) if n and not tries[-1]['marks_ok'] else None
            lost = [w for w in keep if not has_word(tries[-1]['out'], w)] if n else []
            try:
                raw, lp = call(model, system_prompt(terms, has_marks, need, lost, n_ital > 0), send, temp)
            except Exception as e:
                return self._send(502, json.dumps({'error': str(e)}).encode())
            out, js = parse(raw)
            out = ital_from_model(out, n_ital > 0)
            out = inv_unpack(unquote_keep(fix_marks(src, unesc_tags(src, out)), keep), inv)
            ok = marks(src) == marks(out) and bool(out)
            kept = all(has_word(out, w) for w in keep)
            tries.append({'raw': raw, 'out': out, 'json': js, 'marks_ok': ok, 'keep_ok': kept, 'lp': lp})
            if ok and js and kept:
                break
        rank = lambda t: (t['marks_ok'], t['json'], t['keep_ok'])
        best = max(tries, key=rank)               # при равенстве — более ранняя попытка
        content = json.dumps({'ru_translation': best['out']}, ensure_ascii=False) if 'ru_translation' in fmt else best['out']
        self._send(200, json.dumps(reply(content, model), ensure_ascii=False).encode())
        rec = {'src': src, 'terms': terms, 'dropped': dropped, 'keep': keep, 'senses': senses_changed,
               'senses_missing': senses_missing, 'out': best['out'], 'marks_ok': best['marks_ok'],
               'json': best['json'], 'keep_ok': best['keep_ok'], 'retries': len(tries) - 1,
               'raw': [t['raw'] for t in tries]}
        if auto:
            rec['auto_keep'] = auto
        if vterms:
            rec['var_terms'] = [t[0] for t in vterms]
        if n_ital:
            rec['ital'] = [n_ital, len(ITAL.findall(best['out']))]
        if inv:
            rec['inv'] = len(inv)
        if best.get('lp') is not None:
            rec['lp'] = best['lp']
        if REPLAY is not None:
            rec['hybrid'] = 'strong'
        with lock, open(args.log, 'a', encoding='utf-8') as f:
            f.write(json.dumps(rec, ensure_ascii=False) + '\n')
        if REPLAY is not None and args.done:
            with lock, open(args.done, 'a', encoding='utf-8') as f:
                f.write('1\n')
                HYB_DONE[0] += 1
                # шкала bbm считает все абзацы главы (остальные адаптер отдаёт из прохода 1 мгновенно) — свой счёт
                print('\n[гибрид 3/3] модель «качество»: %d из %d отобранных абзацев' % (HYB_DONE[0], len(PICK)), flush=True)

    def log_message(self, *a):
        pass


host, port = args.listen.split(':')
print('rosetta_proxy: %s → %s, журнал %s' % (args.listen, args.target, args.log), flush=True)
print('rosetta_proxy: Context: %s%s' % (args.context, '  (+ logprobs в журнал)' if args.logprobs else ''), flush=True)
http.server.ThreadingHTTPServer((host, int(port)), H).serve_forever()
