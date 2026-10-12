#!/usr/bin/env python3
"""Веб-интерфейс конвейера перевода: http://127.0.0.1:8090

Только стандартная библиотека Python (плюс pdfplumber из образа — число страниц PDF).
Интерфейс ничего не переводит сам: он запускает тот же scripts/translate.sh, что и команда
`docker compose run`, с теми же параметрами (PAGES, ONLY, EN_ONLY, MODEL), и читает то,
что конвейер пишет сам: лог, журнал адаптера Rosetta, отчёты.

- Очередь: одна книга за раз (видеокарта одна), остальные ждут.
- Задачи и логи — в books/.gui (переживают перезапуск). Если контейнер перезапустился во время
  перевода, задача помечается «прервана»; «Повторить» продолжает с места остановки (bbm --resume).
- «Стоп» — сигнал всей группе процессов (bbm, адаптер, скрипты); начатый перевод продолжается повтором.
"""
import html, json, os, re, shutil, signal, subprocess, sys, threading, time, urllib.parse, urllib.request, uuid, zipfile
from html.parser import HTMLParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

SOURCE = os.environ.get('SOURCE_DIR', '/source')
BOOKS = os.environ.get('BOOKS_DIR', '/books')
SCRIPTS = os.environ.get('SCRIPTS', '/scripts')
CONFIG = os.environ.get('CONFIG', '/config')
OLLAMA = os.environ.get('OLLAMA_URL', 'http://ollama:11434')
DOCLING = os.environ.get('DOCLING_URL', 'http://docling:5001')
DEFAULT_MODEL = os.environ.get('MODEL', 'rosetta-ru')
PORT = int(os.environ.get('GUI_PORT', '8090'))
STATE = os.path.join(BOOKS, '.gui')
LOGS = os.path.join(STATE, 'logs')
JOBS_FILE = os.path.join(STATE, 'jobs.json')
HERE = os.path.dirname(os.path.abspath(__file__))

opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
lock = threading.RLock()
jobs = []                   # список задач (dict), порядок = порядок создания
procs = {}                  # id -> Popen
wake = threading.Event()
working = [False]           # поток задач занят (включая ожидание Docling до запуска процесса)


def now():
    return time.time()


# ---------------- хранение ----------------
def save():
    with lock:
        tmp = JOBS_FILE + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(jobs, f, ensure_ascii=False, indent=1)
        os.replace(tmp, JOBS_FILE)


def load():
    global jobs
    os.makedirs(LOGS, exist_ok=True)
    try:
        jobs = json.load(open(JOBS_FILE, encoding='utf-8'))
    except (OSError, ValueError):
        jobs = []
    for j in jobs:                       # контейнер перезапустился посреди работы
        if j['status'] == 'running':
            j['status'], j['finished'] = 'interrupted', now()
            j['note'] = 'Интерфейс перезапускался во время перевода. «Повторить» продолжит с места остановки.'
    save()


def find(jid):
    if jid.startswith('cli-'):
        return next((j for j in external_runs() if j['id'] == jid), None)
    return next((j for j in jobs if j['id'] == jid), None)


# ---------------- перевод, запущенный командой ----------------
# `docker compose run … translate/pdf-epub/translate-pdf` идёт в своём контейнере: интерфейс не видит ни процесса, ни лога
# (09.10: шёл перевод OCP, а в «Сейчас» — «Ничего не переводится», у книги — «перевод прерван»). Признак такого перевода:
# в папке есть .lock (translate.sh пишет его при старте — это время начала) и её файлы менялись последние 5 минут
# (журнал адаптера — каждые несколько секунд). flock на .lock НЕ проверяем: на диске Windows через Docker проба flock
# повисала, а с ней /api/books и вся страница (09.10).
EXT_ACTIVE = 300            # секунд без изменений в папке — перевод больше не считается идущим
_ext = {'t': 0, 'list': [], 'objs': {}}


def last_activity(w):
    t = 0
    for d in (w, os.path.join(w, '.docling')):
        try:
            for e in os.scandir(d):
                if e.is_file(follow_symlinks=False):
                    t = max(t, e.stat().st_mtime)
        except OSError:
            pass
    return t


def book_of(d):
    """Папка в books → (файл книги в source, имя, расширение, страницы, режим) или None."""
    m = re.fullmatch(r'(.+)_epub(?:_p(\d+-\d+))?', d)
    if m and os.path.isfile(os.path.join(SOURCE, m.group(1) + '.pdf')):
        return m.group(1) + '.pdf', m.group(1), 'pdf', m.group(2) or '', 'pdf-epub'
    for ext in ('epub', 'pdf'):
        if os.path.isfile(os.path.join(SOURCE, d + '.' + ext)):
            return d + '.' + ext, d, ext, '', 'epub' if ext == 'epub' else 'pdf'
    return None


def finished_after_lock(w, name):
    """Перевод уже закончился: служебный файл bbm удалён, а отчёт новее начала прогона (.lock)."""
    try:
        start = os.path.getmtime(os.path.join(w, '.lock'))
        qa = os.path.getmtime(os.path.join(w, name + '_qa.txt'))
    except OSError:
        return False
    return qa >= start and not os.path.isfile(os.path.join(w, '.work.temp.bin'))


def external_runs():
    """Переводы, запущенные командой (не интерфейсом), — в виде задач со status 'running' и external=True."""
    if now() - _ext['t'] < 5:
        return _ext['list']
    with lock:
        mine = {x['workdir'] for x in jobs if x['status'] == 'running'}
    res = []
    try:
        dirs = os.listdir(BOOKS)
    except OSError:
        dirs = []
    for d in dirs:
        w = os.path.join(BOOKS, d)
        if d.startswith('.') or w in mine or not os.path.isfile(os.path.join(w, '.lock')):
            continue
        last = last_activity(w)
        if now() - last > 1800:
            continue
        b = book_of(d)
        if not b:
            continue
        file, name, ext, pages, mode = b
        if now() - last > EXT_ACTIVE or finished_after_lock(w, name):
            continue
        try:
            started = os.path.getmtime(os.path.join(w, '.lock'))
        except OSError:
            started = last
        j = _ext['objs'].get(w)
        if j is None or j['started'] != started:        # новый прогон — новая «задача» (прогресс считается заново)
            j = {'id': 'cli-' + d, 'external': True, 'book': file, 'name': name, 'ext': ext, 'mode': mode,
                 'domains': [], 'pages': pages, 'only': [], 'en_only': False, 'status': 'running',
                 'created': started, 'started': started, 'finished': None, 'rc': None, 'workdir': w, 'log': None,
                 'note': 'Запущен командой в PowerShell: лог — в том окне, остановить — Ctrl+C там же.'}
            _ext['objs'][w] = j
        j['model'] = 'rosetta-ru' if os.path.isfile(os.path.join(w, '.rosetta_log.jsonl')) else ''
        res.append(j)
    _ext.update(t=now(), list=res)
    return res


def external_stage(j):
    """Этап перевода, запущенного командой, — по файлам в папке (лога у интерфейса нет)."""
    w = j['workdir']
    try:
        names = set(os.listdir(w))
    except OSError:
        return None
    if j['mode'] == 'pdf':
        return None                                     # PDF → PDF (pdf2zh): этапы другие, шкалы нет
    if j['mode'] == 'pdf-epub' and j['name'] + '.epub' not in names:
        return 'Сборка английского EPUB' if '.docling.json' in names else 'Разметка Docling'
    if '.step.epub' in names:
        return 'Термины и оглавление'
    if 'work_bilingual.epub' in names:
        return 'Восстановление разметки'
    if '.work.temp.bin' in names or '.rosetta_log.jsonl' in names:
        return 'Перевод'
    if 'work.epub' in names:
        return 'Подготовка книги'
    return 'Глоссарий'


# ---------------- книги ----------------
_pages_cache = {}


def pdf_pages(path):
    st = os.stat(path)
    key = (path, st.st_mtime, st.st_size)
    if key not in _pages_cache:
        try:
            import pdfplumber
            with pdfplumber.open(path) as p:
                _pages_cache[key] = len(p.pages)
        except Exception:
            _pages_cache[key] = None
    return _pages_cache[key]


def workdir(name, ext, pages=''):
    if ext == 'pdf':
        return os.path.join(BOOKS, '%s_epub%s' % (name, ('_p' + pages) if pages else ''))
    return os.path.join(BOOKS, name)


_chapters_cache = {}


def epub_chapters(path):
    """[(файл, название)] по порядку чтения; указатель и оглавление не предлагаем. Кэш по размеру и времени файла:
    без него /api/books раз в 30 с читал каждую книгу целиком (на диске Windows через Docker это секунды)."""
    try:
        st = os.stat(path)
    except OSError:
        return []
    key = (path, st.st_mtime, st.st_size)
    if key not in _chapters_cache:
        _chapters_cache[key] = _epub_chapters(path)
    return _chapters_cache[key]


def _epub_chapters(path):
    try:
        z = zipfile.ZipFile(path)
        cont = z.read('META-INF/container.xml').decode('utf-8', 'replace')
        opf = re.search(r'full-path="([^"]+)"', cont).group(1)
        o = z.read(opf).decode('utf-8', 'replace')
        base = os.path.dirname(opf)
        items = {}
        for m in re.finditer(r'<item\b([^>]*)/?>', o):
            a = dict(re.findall(r'([\w:-]+)="([^"]*)"', m.group(1)))
            items[a.get('id')] = a
        out = []
        for idref in re.findall(r'<itemref\b[^>]*idref="([^"]+)"', o):
            a = items.get(idref) or {}
            href = a.get('href', '')
            if not href or 'nav' in (a.get('properties') or ''):
                continue
            full = os.path.normpath(os.path.join(base, href)).replace('\\', '/')
            try:
                t = z.read(full).decode('utf-8', 'replace')
            except KeyError:
                continue
            if re.search(r'data-type="index"|epub:type="[^"]*\bindex\b', t):
                continue
            m = re.search(r'<h[1-3][^>]*>(.*?)</h[1-3]>', t, re.S) or re.search(r'<title>(.*?)</title>', t, re.S)
            title = re.sub(r'\s+', ' ', html.unescape(re.sub(r'<[^>]+>', '', m.group(1)))).strip() if m else ''
            out.append((os.path.basename(href), title or os.path.basename(href)))
        return out
    except Exception:
        return []


def outputs(w):
    if not os.path.isdir(w):
        return []
    res = []
    for f in sorted(os.listdir(w)):
        p = os.path.join(w, f)
        if f.startswith(('.', 'work')) or not os.path.isfile(p):      # служебные файлы конвейера не показываем
            continue
        res.append({'name': f, 'size': os.path.getsize(p), 'url': '/files/' + urllib.parse.quote(os.path.relpath(p, BOOKS))})
    return res


def en_epub(name, ext, pages=''):
    return os.path.join(SOURCE, name + '.epub') if ext == 'epub' else os.path.join(workdir(name, ext, pages), name + '.epub')


def ru_epub(name, ext, pages=''):
    return os.path.join(workdir(name, ext, pages), name + '_ru.epub')


def page_runs(name, ext):
    """Переводы части PDF по страницам: [(страницы '1-40', папка)] — books/<имя>_epub_p1-40 и т. п."""
    if ext != 'pdf' or not os.path.isdir(BOOKS):
        return []
    pre = name + '_epub_p'
    res = []
    for d in os.listdir(BOOKS):
        if d.startswith(pre) and re.fullmatch(r'\d+-\d+', d[len(pre):]) and os.path.isdir(os.path.join(BOOKS, d)):
            res.append((d[len(pre):], os.path.join(BOOKS, d)))
    return sorted(res, key=lambda r: tuple(map(int, r[0].split('-'))))


def split_run(s):
    """'книга.pdf@1-40' → ('книга.pdf', '1-40'): часть книги по страницам в «Просмотре»."""
    b, at, p = s.rpartition('@')
    if at and re.fullmatch(r'\d+-\d+', p):
        return b, p
    return s, ''


def _chapter_texts(path):
    """{имя файла главы: текст вне кода} для глав книги (без оглавления и указателя)."""
    res = {}
    z = zipfile.ZipFile(path)
    for f in z.namelist():
        base = os.path.basename(f)
        if not f.endswith(('.xhtml', '.html', '.htm')) or base.startswith('nav'):
            continue
        t = z.read(f).decode('utf-8', 'replace')
        if re.search(r'data-type="index"|epub:type="[^"]*\bindex\b|<nav\b', t):
            continue
        p = _Leaves()
        p.feed(t)
        res[base] = ' '.join(p.out)
    return res


_cov_cache = {}


def coverage(name, ext):
    """Сколько глав переведено: глава с текстом (50+ букв) считается переведённой, если в ней больше 30% кириллицы."""
    en, ru = en_epub(name, ext), ru_epub(name, ext)
    if not (os.path.isfile(en) and os.path.isfile(ru)):
        return None
    key = (en, os.path.getmtime(en), ru, os.path.getmtime(ru))
    if _cov_cache.get(name, (None,))[0] != key:
        try:
            E, R = _chapter_texts(en), _chapter_texts(ru)
            files = {}
            for f, t in E.items():
                if len(re.findall(r'[A-Za-z]', t)) < 50:
                    continue                          # обложка, пустые страницы — не считаем
                r = R.get(f, '')
                cyr, lat = len(re.findall('[А-Яа-яЁё]', r)), len(re.findall(r'[A-Za-z]', r))
                files[f] = cyr > 0.3 * (cyr + lat) if cyr + lat else False
            cov = {'files': files, 'done': sum(files.values()), 'total': len(files)}
        except Exception:
            cov = None
        _cov_cache[name] = (key, cov)
    return _cov_cache[name][1]


def book_state(name, ext, runs=()):
    if any(x['book'] == name + '.' + ext for x in external_runs()):
        return 'в процессе'
    with lock:
        act = {x['status'] for x in jobs if x['book'] == name + '.' + ext and x['status'] in ('queued', 'running')}
    if 'running' in act:
        return 'в процессе'
    if 'queued' in act:
        return 'в очереди'
    w = workdir(name, ext)
    if os.path.isfile(os.path.join(w, '.work.temp.bin')):
        return 'прерван — можно продолжить'
    if os.path.isfile(ru_epub(name, ext)):
        cov = coverage(name, ext)
        if cov and cov['total'] and cov['done'] < cov['total']:
            return 'переведена частично'
        return 'переведена'
    if any(os.path.isfile(os.path.join(rw, name + '_ru.epub')) for _, rw in runs):
        return 'переведены страницы'          # переведена только часть PDF по страницам (отдельная папка)
    if any(os.path.isfile(os.path.join(rw, '.work.temp.bin')) for _, rw in runs):
        return 'прерван — можно продолжить'
    if ext == 'pdf' and os.path.isfile(os.path.join(w, name + '.epub')):
        return 'собран английский EPUB'
    return 'не начата'


def books():
    res = []
    for f in sorted(os.listdir(SOURCE)) if os.path.isdir(SOURCE) else []:
        name, dot, ext = f.rpartition('.')
        ext = ext.lower()
        if not dot or ext not in ('epub', 'pdf'):
            continue
        path = os.path.join(SOURCE, f)
        runs = page_runs(name, ext)
        b = {'file': f, 'name': name, 'ext': ext, 'size': os.path.getsize(path), 'state': book_state(name, ext, runs),
             'glossary': os.path.isfile(os.path.join(SOURCE, name + '.glossary.csv'))}
        w = workdir(name, ext)
        if ext == 'pdf':
            b['pages'] = pdf_pages(path)
            en = os.path.join(w, name + '.epub')
            b['chapters'] = epub_chapters(en) if os.path.isfile(en) else []
        else:
            b['chapters'] = epub_chapters(path)
        # файлы и «Просмотр»: основная папка и части по страницам
        b['outputs'] = outputs(w) + [dict(o, name='стр. %s · %s' % (p, o['name'])) for p, rw in runs for o in outputs(rw)]
        b['page_runs'] = [p for p, rw in runs if os.path.isfile(os.path.join(rw, name + '_ru.epub'))]
        cov = coverage(name, ext) if b['state'].startswith('переведена') else None
        if cov:
            b['coverage'] = {'done': cov['done'], 'total': cov['total'],
                             'translated': [f for f, ok in cov['files'].items() if ok]}
        b['views'] = ([{'id': f, 'label': f}] if os.path.isfile(en_epub(name, ext)) else []) + \
                     [{'id': f + '@' + p, 'label': '%s · стр. %s' % (f, p.replace('-', '–'))}
                      for p, rw in runs if os.path.isfile(os.path.join(rw, name + '.epub'))]
        b['viewable'] = bool(b['views'])
        res.append(b)
    return res


def glossaries():
    d = os.path.join(CONFIG, 'glossary')
    res = []
    for f in sorted(os.listdir(d)) if os.path.isdir(d) else []:
        if not f.endswith('.csv') or f in ('keep.csv',):
            continue
        try:
            n = sum(1 for line in open(os.path.join(d, f), encoding='utf-8-sig') if line.strip()) - 1
        except OSError:
            n = 0
        res.append({'name': f[:-4], 'terms': max(n, 0), 'always': f == 'base.csv'})
    return res


# ---------------- подсказка областей глоссария ----------------
# Считается в фоне (у большой книги 20–45 с на CPU), по одной книге за раз; результат кэшируется в books/.gui/detect
# и пересчитывается, если изменились книга, её английский EPUB (для PDF) или глоссарии.
DETECT_DIR = os.path.join(STATE, 'detect')
detect_lock = threading.Lock()
detect_running = set()


def _detect_source(name, ext):
    """Для PDF лучше полный текст собранного английского EPUB, если он уже есть; иначе выборка страниц PDF."""
    if ext == 'pdf':
        en = os.path.join(workdir(name, ext), name + '.epub')
        if os.path.isfile(en):
            return en
    return os.path.join(SOURCE, name + '.' + ext)


def _detect_key(path):
    gdir = os.path.join(CONFIG, 'glossary')
    g = sorted((f, os.path.getmtime(os.path.join(gdir, f))) for f in os.listdir(gdir) if f.endswith('.csv')) \
        if os.path.isdir(gdir) else []
    st = os.stat(path)
    dd = os.path.join(SCRIPTS, 'detect_domains.py')       # новые правила подбора — пересчитать подсказки
    return json.dumps([path, st.st_mtime, st.st_size, g, os.path.getmtime(dd) if os.path.isfile(dd) else 0])


def _detect_run(book, path, key, cache):
    try:
        if SCRIPTS not in sys.path:
            sys.path.insert(0, SCRIPTS)
        import detect_domains
        with detect_lock:
            r = detect_domains.detect(path, os.path.join(CONFIG, 'glossary'))
        r['key'], r['from'] = key, os.path.basename(path)
        os.makedirs(DETECT_DIR, exist_ok=True)
        with open(cache + '.tmp', 'w', encoding='utf-8') as f:
            json.dump(r, f, ensure_ascii=False)
        os.replace(cache + '.tmp', cache)
    except Exception as e:
        print('подсказка областей для %s: %s' % (book, e), flush=True)
    finally:
        detect_running.discard(book)


def detect(book):
    name, _, ext = book.rpartition('.')
    ext = ext.lower()
    if not name or '/' in book or ext not in ('epub', 'pdf') or not os.path.isfile(os.path.join(SOURCE, book)):
        raise ValueError('Нет такой книги')
    path = _detect_source(name, ext)
    key = _detect_key(path)
    cache = os.path.join(DETECT_DIR, book + '.json')
    try:
        r = json.load(open(cache, encoding='utf-8'))
        if r.get('key') == key:
            r.pop('key', None)
            r['status'] = 'ready'
            return r
    except (OSError, ValueError):
        pass
    if book not in detect_running:
        detect_running.add(book)
        threading.Thread(target=_detect_run, args=(book, path, key, cache), daemon=True).start()
    return {'status': 'pending'}


def http_json(url, data=None, timeout=3):
    req = urllib.request.Request(url, data=data, method='POST' if data is not None else 'GET',
                                 headers={'Content-Type': 'application/json'} if data is not None else {})
    with opener.open(req, timeout=timeout) as r:
        return json.loads(r.read() or b'{}')


def models():
    try:
        tags = http_json(OLLAMA + '/api/tags')['models']
    except Exception:
        return [DEFAULT_MODEL]
    names = [m['name'].split(':latest')[0] for m in tags]
    names = [n for n in names if not n.startswith('hf.co/') and n != 'translategemma:4b']
    return sorted(names, key=lambda n: (n != DEFAULT_MODEL, n))


# ---------------- загрузка компьютера ----------------
# CPU и RAM — виртуальной машины Docker (WSL): все процессы перевода живут в ней, и именно её лимит памяти
# (.wslconfig) приводит к «Cannot allocate memory». GPU — через nvidia-smi (контейнеру дан доступ к GPU).
usage = {'cpu': None, 'ram': None, 'gpu': None, 'ollama': [], 'docling': False}


def _cpu_times():
    with open('/proc/stat') as f:
        v = [int(x) for x in f.readline().split()[1:9]]
    return sum(v), v[3] + v[4]                  # всего, простой (idle + iowait)


def _ram():
    m = {}
    with open('/proc/meminfo') as f:
        for line in f:
            k, _, v = line.partition(':')
            m[k] = int(v.split()[0]) * 1024
    total = m['MemTotal']
    used = total - m.get('MemAvailable', m['MemFree'])
    return {'pct': round(100 * used / total), 'used': used, 'total': total}


def _nvidia_smi():
    for exe in (shutil.which('nvidia-smi'), '/usr/lib/wsl/lib/nvidia-smi', '/usr/bin/nvidia-smi'):
        if exe and os.path.isfile(exe):
            return exe
    return None


def _gpu(exe):
    r = subprocess.run([exe, '--query-gpu=utilization.gpu,memory.used,memory.total', '--format=csv,noheader,nounits'],
                       capture_output=True, text=True, timeout=5)
    if r.returncode != 0 or not r.stdout.strip():
        return None
    num = lambda x: float(x) if re.fullmatch(r'\s*[\d.]+\s*', x) else None
    u, mu, mt = (num(x) for x in r.stdout.splitlines()[0].split(','))
    return {'pct': round(u) if u is not None else None,
            'mem_used': mu * 1048576 if mu is not None else None, 'mem_total': mt * 1048576 if mt is not None else None}


def sampler():
    """Раз в 2 секунды: загрузка CPU, память, GPU. /api/status отдаёт последние значения сразу."""
    exe = _nvidia_smi()
    try:
        prev = _cpu_times()
    except OSError:
        prev = None
    while True:
        time.sleep(2)
        try:
            cur = _cpu_times()
            if prev and cur[0] > prev[0]:
                usage['cpu'] = max(0, min(100, round(100 * (1 - (cur[1] - prev[1]) / (cur[0] - prev[0])))))
            prev = cur
        except OSError:
            usage['cpu'] = None
        try:
            usage['ram'] = _ram()
        except (OSError, KeyError, ValueError):
            usage['ram'] = None
        try:
            usage['gpu'] = _gpu(exe) if exe else None
        except Exception:
            usage['gpu'] = None


# Ollama и Docling опрашиваются в своих потоках: выключенный Docling отвечает не сразу (поиск его адреса в сети
# Docker занимает секунды), а /api/status должен отвечать мгновенно — по нему значок в трее проверяет, что интерфейс жив.
def probe_ollama():
    while True:
        try:
            ps = http_json(OLLAMA + '/api/ps')['models']
            usage['ollama'] = [{'name': m['name'], 'size': m.get('size', 0), 'vram': m.get('size_vram', 0),
                                'gpu': round(100 * m.get('size_vram', 0) / m['size']) if m.get('size') else 0} for m in ps]
        except Exception:
            usage['ollama'] = None
        time.sleep(3)


def probe_docling():
    while True:
        try:
            with opener.open(DOCLING + '/health', timeout=2) as r:
                usage['docling'] = r.status == 200
        except Exception:
            usage['docling'] = False
        time.sleep(3)


def system_status():
    """Последние значения из фоновых потоков — без сетевых запросов."""
    return {'ollama': usage['ollama'], 'docling': usage['docling'], 'cpu': usage['cpu'], 'ram': usage['ram'],
            'gpu': usage['gpu'], 'cores': os.cpu_count()}


# ---------------- прогресс ----------------
BLOCK = set('address article aside blockquote body caption dd details div dl dt fieldset figcaption figure footer '
            'form h1 h2 h3 h4 h5 h6 header hr html li main nav ol p pre section summary table tbody td tfoot th '
            'thead tr ul'.split())
SKIP = {'pre', 'code', 'script', 'style', 'head', 'title', 'svg', 'math', 'var', 'sup', 'sub'}


class _Units(HTMLParser):
    """Число абзацев, которые bbm отправит модели (блок с текстом вне кода). Сверено: 32 из 32, 459 из 460."""
    def __init__(self):
        super().__init__()
        self.stack, self.skip, self.units = [], 0, 0

    def handle_starttag(self, t, a):
        if t in SKIP:
            self.skip += 1
        if t in BLOCK:
            self.stack.append([t, False])

    def handle_endtag(self, t):
        if t in SKIP and self.skip:
            self.skip -= 1
        if t in BLOCK:
            while self.stack:
                x = self.stack.pop()
                self.units += x[1]
                if x[0] == t:
                    break

    def handle_data(self, d):
        if not self.skip and self.stack and re.search(r'[A-Za-z]{2}', d):
            self.stack[-1][1] = True


def count_units(epub, only):
    z = zipfile.ZipFile(epub)
    n = 0
    for f in z.namelist():
        if not f.endswith(('.xhtml', '.html', '.htm')) or os.path.basename(f).startswith('nav'):
            continue
        if only and os.path.basename(f) not in only:
            continue
        t = z.read(f).decode('utf-8', 'replace')
        if re.search(r'data-type="index"|epub:type="[^"]*\bindex\b|<nav\b', t):
            continue
        p = _Units()
        p.feed(t)
        n += p.units
    return n


_lines_cache = {}


def count_lines(path):
    try:
        st = os.stat(path)
    except OSError:
        return 0
    c = _lines_cache.get(path)
    if c and c[0] == st.st_size:
        return c[1]
    start, n = (c[0], c[1]) if c and c[0] < st.st_size else (0, 0)
    with open(path, 'rb') as f:
        f.seek(start)
        n += f.read().count(b'\n')
    _lines_cache[path] = (st.st_size, n)
    return n


# Этапы — только строки самих скриптов: log() пишет «[ЧЧ:ММ:СС] текст». В лог идёт и переведённый текст (bbm печатает
# каждый абзац), поэтому без привязки к началу строки «кнопка «Готово»» в книге показывалась как конец перевода.
_TS = r'^\[\d\d:\d\d:\d\d\] '
STAGES = [(_TS + r'A/B Разметка Docling', 'Разметка Docling'), (_TS + r'B/B Сборка английского', 'Сборка английского EPUB'),
          (_TS + r'1/6 Глоссарий', 'Глоссарий'), (_TS + r'2/6 ', 'Подготовка книги'), (_TS + r'3/6 Перевод', 'Перевод'),
          (_TS + r'4/6 Восстановление', 'Восстановление разметки'), (_TS + r'5/6 ', 'Термины и оглавление'),
          (_TS + r'6/6 Проверка', 'Проверка'), (_TS + r'Готово[ :(]', 'Готово'), (_TS + r'ОШИБКА', 'Ошибка')]


def tail(path, nbytes=40000):
    try:
        with open(path, 'rb') as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - nbytes))
            return f.read().decode('utf-8', 'replace')
    except OSError:
        return ''


def scan_log(j):
    """Этап и ход разметки Docling — по ВСЕМУ логу, а не по хвосту. bbm печатает в лог каждый абзац (EN и RU),
    и за 5–10 минут перевода строка «3/6 Перевод» уходит из последних 200 КБ: этап пропадал, окно «Сейчас»
    показывало «запуск…» без шкалы (08.10). Читаем только новое с прошлого раза; позиция и найденное — в j['_scan']."""
    st = dict(j.get('_scan') or {})
    try:
        size = os.path.getsize(j['log'])
    except OSError:
        return st
    pos = st.get('pos', 0)
    if size < pos:                       # лог пересоздан — читаем заново
        st, pos = {}, 0
    if size > pos:
        with open(j['log'], 'rb') as f:
            f.seek(pos)
            data = f.read(size - pos)
        cut = data.rfind(b'\n') + 1      # неполную последнюю строку дочитаем в следующий раз
        for line in data[:cut].decode('utf-8', 'replace').splitlines():
            # строка прогресса bbm (tqdm): «… | 9499/9510 [3:52:24<01:06, …» — точное число абзацев (09.10:
            # своя оценка count_units давала 9331 из 9510, и шкала упиралась в 100% за час до конца)
            for m in re.finditer(r'\|\s*(\d+)/(\d+) \[', line):
                st['bbm_total'] = int(m.group(2))
            if line.startswith('['):
                for pat, name in STAGES:
                    if re.search(pat, line):
                        st['stage'] = name
            if 'docling:' in line:
                m = re.search(r'docling: стр\. .*\((\d+)/(\d+), осталось ~(\d+) мин', line)
                if m:
                    st['docling'] = [int(m.group(1)), int(m.group(2)), int(m.group(3))]
                m = re.search(r'docling: страницы .*кусков (\d+) \(готово раньше: (\d+)\)', line)
                if m and 'docling' not in st:
                    st['docling'] = [int(m.group(2)), int(m.group(1)), None]
        st['pos'] = pos + cut
        j['_scan'] = st                  # целиком новым словарём: save() может в это время писать jobs.json
    return st


def progress(j):
    st = {'stage': external_stage(j)} if j.get('external') else scan_log(j)
    p = {'stage': st.get('stage')}
    if 'docling' in st:
        p['docling'] = st['docling']
    w = j['workdir']
    work = os.path.join(w, 'work.epub')
    # Абзацы считаем только на этапе «Перевод»: на «Подготовке» prep_epub ещё пишет work.epub, недописанный zip
    # давал ошибку → units = 0 навсегда, и строки «N из ~M абзацев» не было весь перевод (08.10, OCP).
    # Ошибку или 0 пробуем снова не чаще раза в минуту.
    # Считаем в фоне: на большой книге это секунды (9331 абзац OCP), и /api/jobs не должен их ждать (09.10).
    if not j.get('units') and os.path.isfile(work) and p['stage'] == 'Перевод' \
            and now() - j.get('_units_try', 0) > 60:
        j['_units_try'] = now()

        def calc(j=j, only=set(j['only']) if j.get('only') else None):
            try:
                j['units'] = count_units(work, only)
            except Exception:
                j['units'] = None
        threading.Thread(target=calc, daemon=True).start()
    if st.get('bbm_total') and p['stage'] == 'Перевод':
        j['units'] = st['bbm_total']
        p['exact'] = True
    if j.get('units'):
        done = count_lines(os.path.join(w, '.rosetta_log.jsonl')) if j['model'].startswith('rosetta') else None
        p['total'] = j['units']
        if done is not None:
            p['done'] = done
            if done >= j['units'] and not p.get('exact'):
                p['over'] = True             # оценка занижена (bbm режет абзацы мельче): не показываем «100%, ~0 мин»
            hist = j.setdefault('_rate', [])
            if not hist or hist[-1][1] != done:
                hist.append((now(), done))
                del hist[:-60]
            if len(hist) >= 2 and hist[-1][1] > hist[0][1]:
                rate = (hist[-1][1] - hist[0][1]) / (hist[-1][0] - hist[0][0])
                p['eta_min'] = round(max(0, j['units'] - done) / rate / 60) if rate > 0 and not p.get('over') else None
                p['per_min'] = round(rate * 60, 1)
    return p


def public(j, with_progress=False):
    d = {k: v for k, v in j.items() if not k.startswith('_')}
    if with_progress:
        d['progress'] = progress(j)
    d['outputs'] = outputs(j['workdir']) if j['status'] in ('done', 'failed', 'stopped', 'interrupted') else []
    # нужен ли этой задаче Docling (PDF, английский EPUB ещё не собран) — по этому признаку значок в трее его включает
    d['needs_docling'] = (j['mode'] == 'pdf-epub' and j['status'] in ('queued', 'running')
                          and not os.path.isfile(os.path.join(j['workdir'], j['name'] + '.epub')))
    return d


# ---------------- запуск ----------------
def new_job(p):
    book = p.get('book', '')
    path = os.path.join(SOURCE, book)
    if not book or '/' in book or not os.path.isfile(path):
        raise ValueError('Нет такой книги в source: %s' % book)
    name, _, ext = book.rpartition('.')
    ext = ext.lower()
    pages = (p.get('pages') or '').strip() if ext == 'pdf' else ''
    if pages and not re.fullmatch(r'\d+-\d+', pages):
        raise ValueError('Страницы — в виде 170-229')
    if pages:
        a, b = map(int, pages.split('-'))
        n = pdf_pages(path)
        if a < 1 or b < a or (n and b > n):
            raise ValueError('Страницы: от 1 до %s, «от» не больше «до»' % n)
    known = {g['name'] for g in glossaries()}
    domains = [d for d in p.get('domains', []) if d in known and d != 'base']
    only = [f for f in p.get('only', []) if re.fullmatch(r'[\w.\-]+', f)]
    model = p.get('model') or DEFAULT_MODEL
    if not re.fullmatch(r'[\w.:\-/]+', model):
        raise ValueError('Странное имя модели')
    with lock:
        if any(x['book'] == book for x in external_runs()):
            raise ValueError('Эта книга сейчас переводится командой в PowerShell — дождись окончания')
        busy = [x for x in jobs if x['status'] in ('queued', 'running') and x['book'] == book]
        if busy:
            raise ValueError('Эта книга уже в очереди или переводится')
        jid = time.strftime('%Y%m%d-%H%M%S-') + uuid.uuid4().hex[:4]
        j = {'id': jid, 'book': book, 'name': name, 'ext': ext, 'mode': 'pdf-epub' if ext == 'pdf' else 'epub',
             'domains': domains, 'pages': pages, 'only': only, 'en_only': bool(p.get('en_only')) and ext == 'pdf',
             'model': model, 'status': 'queued', 'created': now(), 'started': None, 'finished': None, 'rc': None,
             'workdir': workdir(name, ext, pages), 'log': os.path.join(LOGS, jid + '.log'), 'note': ''}
        jobs.append(j)
        save()
    wake.set()
    return j


def run(j):
    if j['ext'] == 'pdf' and not os.path.isfile(os.path.join(j['workdir'], j['name'] + '.epub')):
        # Docling включает значок в трее, когда в очереди есть PDF. Обычно это секунды; до 30 минут — если образ
        # не скачан заранее (установка без app\install.ps1) и docker compose качает его сейчас
        t0 = now()
        while not system_status()['docling'] and now() - t0 < 1800:
            with lock:
                j['note'] = 'Жду запуска разметки PDF…'
            time.sleep(5)
        j['note'] = ''
    env = dict(os.environ)
    env.update({'MODEL': j['model'], 'PYTHONUNBUFFERED': '1'})
    for k in ('PAGES', 'ONLY', 'EN_ONLY', 'PIPELINE'):
        env.pop(k, None)
    if j['pages']:
        env['PAGES'] = j['pages']
    if j['only']:
        env['ONLY'] = ','.join(j['only'])
    if j['en_only']:
        env['EN_ONLY'] = '1'
    if j['ext'] == 'pdf':
        env['PIPELINE'] = 'docling'
    cmd = ['sh', os.path.join(SCRIPTS, 'translate.sh'), j['book'], ','.join(j['domains']) or 'base']
    with open(j['log'], 'a', encoding='utf-8') as log:
        log.write('[интерфейс] %s: %s  (MODEL=%s PAGES=%s ONLY=%s EN_ONLY=%s)\n' % (
            time.strftime('%Y-%m-%d %H:%M:%S'), ' '.join(cmd[2:]), j['model'], j['pages'] or '-',
            env.get('ONLY', '-'), env.get('EN_ONLY', '-')))
        log.flush()
        pr = subprocess.Popen(cmd, stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                              env=env, start_new_session=True)
        with lock:
            procs[j['id']] = pr
            j['status'], j['started'], j['pid'] = 'running', now(), pr.pid
            save()
        rc = pr.wait()
    with lock:
        procs.pop(j['id'], None)
        j['rc'], j['finished'] = rc, now()
        if j.get('_stop'):
            j['status'] = 'stopped'
            j['note'] = 'Остановлено. «Повторить» продолжит с места остановки.'
        else:
            j['status'] = 'done' if rc == 0 else 'failed'
            if rc != 0:
                lines = tail(j['log'], 20000).splitlines()
                j['note'] = next((l for l in reversed(lines) if re.match(_TS + 'ОШИБКА', l)), None) or \
                    next((l for l in reversed(lines) if re.search(r'\b\w*Error\b', l)), 'Код выхода %d — см. лог' % rc)
        save()


def worker():
    while True:
        with lock:
            j = next((x for x in jobs if x['status'] == 'queued'), None)
            working[0] = j is not None
        if j is None:
            wake.wait(5)
            wake.clear()
            continue
        if external_runs():                  # видеокарта занята переводом, запущенным командой: два сразу не влезут в 4 ГБ
            with lock:
                working[0] = False
                if j['status'] == 'queued' and not j.get('note'):
                    j['note'] = 'Жду: идёт перевод, запущенный командой в PowerShell'
                    save()
            wake.wait(10)
            wake.clear()
            continue
        with lock:
            if j.get('note', '').startswith('Жду: идёт перевод'):
                j['note'] = ''
        try:
            run(j)
        except Exception as e:           # не даём потоку умереть
            with lock:
                j['status'], j['finished'], j['note'] = 'failed', now(), 'Интерфейс не смог запустить: %s' % e
                save()
        finally:
            with lock:
                working[0] = False


def autoreload():
    """После обновления server.py (или detect_domains.py, который он импортирует) перезапускаемся сами, как только
    нет идущего перевода: перезапуск контейнера вручную не нужен, а идущий перевод не прерывается."""
    me = os.path.abspath(__file__)
    files = [me, os.path.join(SCRIPTS, 'detect_domains.py')]
    mt = lambda: [os.path.getmtime(f) if os.path.isfile(f) else 0 for f in files]
    m0 = mt()
    while True:
        time.sleep(5)
        try:
            if mt() == m0:
                continue
            for f in files:
                if os.path.isfile(f):
                    compile(open(f, encoding='utf-8').read(), f, 'exec')    # файлы дописаны и без ошибок
        except (OSError, SyntaxError, ValueError):
            continue
        with lock:
            if working[0] or procs:
                continue
            print('server.py обновлён — перезапускаю интерфейс', flush=True)
            save()
            os.execv(sys.executable, [sys.executable] + sys.argv)


def stop(jid):
    if jid.startswith('cli-'):
        raise ValueError('Перевод запущен командой — остановить его можно Ctrl+C в окне PowerShell')
    with lock:
        j = find(jid)
        if not j:
            raise ValueError('Нет такой задачи')
        if j['status'] == 'queued':
            j['status'], j['finished'], j['note'] = 'cancelled', now(), 'Убрана из очереди'
            save()
            return
        pr = procs.get(jid)
        if not pr:
            raise ValueError('Задача не выполняется')
        j['_stop'] = True
    try:
        os.killpg(pr.pid, signal.SIGTERM)
    except ProcessLookupError:
        return

    def force():
        time.sleep(15)
        if pr.poll() is None:
            try:
                os.killpg(pr.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
    threading.Thread(target=force, daemon=True).start()


def retry(jid):
    if jid.startswith('cli-'):
        raise ValueError('Перевод запущен командой — повтори ту же команду в PowerShell')
    j = find(jid)
    if not j:
        raise ValueError('Нет такой задачи')
    return new_job({'book': j['book'], 'domains': j['domains'], 'pages': j['pages'], 'only': j['only'],
                    'model': j['model'], 'en_only': j['en_only']})


def pairs(jid, offset, limit, bad_only):
    j = find(jid)
    if not j:
        raise ValueError('Нет такой задачи')
    path = os.path.join(j['workdir'], '.rosetta_log.jsonl')
    rows = []
    try:
        with open(path, encoding='utf-8') as f:
            for i, line in enumerate(f):
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                if bad_only and r.get('marks_ok', True) and r.get('json', True) and not r.get('retries'):
                    continue
                rows.append({'n': i + 1, 'src': r.get('src', ''), 'out': r.get('out', ''), 'marks_ok': r.get('marks_ok'),
                             'json': r.get('json'), 'retries': r.get('retries', 0), 'terms': r.get('terms', [])})
    except OSError:
        pass
    return {'total': len(rows), 'rows': rows[offset:offset + limit]}


class _Leaves(HTMLParser):
    """Тексты «листовых» блоков (абзац, пункт, ячейка, заголовок) вне кода — как их видит bbm."""
    def __init__(self):
        super().__init__()
        self.stack, self.skip, self.out = [], 0, []

    def handle_starttag(self, t, a):
        if t in SKIP:
            self.skip += 1
        if t in BLOCK:
            self.stack.append([t, []])

    def handle_endtag(self, t):
        if t in SKIP and self.skip:
            self.skip -= 1
        if t in BLOCK:
            while self.stack:
                tag, parts = self.stack.pop()
                txt = re.sub(r'\s+', ' ', ''.join(parts)).strip()
                if txt:
                    self.out.append(txt)
                if tag == t:
                    break

    def handle_data(self, d):
        if not self.skip and self.stack:
            self.stack[-1][1].append(d)


def untranslated(jid):
    """Абзацы, оставшиеся в переводе на английском (есть латиница, нет кириллицы). Указатель не переводится — не берём."""
    j = find(jid)
    if not j:
        raise ValueError('Нет такой задачи')
    ru = os.path.join(j['workdir'], j['name'] + '_ru.epub')
    if not os.path.isfile(ru) or j['status'] == 'running':
        return {'ready': False, 'rows': []}
    rows = []
    z = zipfile.ZipFile(ru)
    for f in z.namelist():
        base = os.path.basename(f)
        if not f.endswith(('.xhtml', '.html', '.htm')) or base.startswith('nav'):
            continue
        if j.get('only') and base not in j['only']:
            continue
        t = z.read(f).decode('utf-8', 'replace')
        if re.search(r'data-type="index"|epub:type="[^"]*\bindex\b|<nav\b', t):
            continue
        p = _Leaves()
        p.feed(t)
        for txt in p.out:
            if len(re.findall(r'[A-Za-z]', txt)) >= 20 and not re.search('[А-Яа-яЁё]', txt):
                rows.append({'file': base, 'text': txt})
    rows.sort(key=lambda r: r['file'])
    return {'ready': True, 'rows': rows}


def _members(path):
    """{имя файла главы: полный путь внутри zip} по порядку чтения."""
    z = zipfile.ZipFile(path)
    cont = z.read('META-INF/container.xml').decode('utf-8', 'replace')
    opf = re.search(r'full-path="([^"]+)"', cont).group(1)
    o = z.read(opf).decode('utf-8', 'replace')
    base = os.path.dirname(opf)
    items = {}
    for m in re.finditer(r'<item\b([^>]*)/?>', o):
        a = dict(re.findall(r'([\w:-]+)="([^"]*)"', m.group(1)))
        items[a.get('id')] = a
    res = {}
    for idref in re.findall(r'<itemref\b[^>]*idref="([^"]+)"', o):
        a = items.get(idref) or {}
        if a.get('href') and 'nav' not in (a.get('properties') or ''):
            full = os.path.normpath(os.path.join(base, urllib.parse.unquote(a['href']))).replace('\\', '/')
            res[os.path.basename(full)] = full
    return res


def view_info(book):
    book, pages = split_run(book)
    b = next((x for x in books() if x['file'] == book), None)
    if not b:
        raise ValueError('Нет такой книги')
    pages = pages if b['ext'] == 'pdf' else ''
    en, ru = en_epub(b['name'], b['ext'], pages), ru_epub(b['name'], b['ext'], pages)
    if not os.path.isfile(en):
        return {'chapters': [], 'has_ru': False}
    E = _members(en)
    R = _members(ru) if os.path.isfile(ru) else {}
    titles = dict(epub_chapters(en))
    ch = [{'file': f, 'title': titles.get(f, f), 'en': m, 'ru': R.get(f)} for f, m in E.items() if f in titles]
    return {'chapters': ch, 'has_ru': bool(R)}


def epub_file(side, book, member):
    """Файл из EPUB оригинала или перевода — чтобы окно просмотра показывало главы с картинками и стилями книги."""
    book, pages = split_run(book)
    name, _, ext = book.rpartition('.')
    ext = ext.lower()
    if not name or '/' in book or ext not in ('epub', 'pdf') or not os.path.isfile(os.path.join(SOURCE, book)):
        return None
    pages = pages if ext == 'pdf' else ''
    path = en_epub(name, ext, pages) if side == 'en' else ru_epub(name, ext, pages)
    if not os.path.isfile(path):
        return None
    try:
        return zipfile.ZipFile(path).read(member)
    except KeyError:
        return None


def clear_history():
    with lock:
        keep = [j for j in jobs if j['status'] in ('queued', 'running')]
        for j in jobs:
            if j not in keep:
                try:
                    os.remove(j['log'])
                except OSError:
                    pass
        n = len(jobs) - len(keep)
        jobs[:] = keep
        save()
    return n


# ---------------- HTTP ----------------
class H(BaseHTTPRequestHandler):
    server_version = 'book_translate_gui'

    def log_message(self, *a):
        pass

    def send(self, code, body, ctype='application/json; charset=utf-8', extra=None):
        if isinstance(body, (dict, list)):
            body = json.dumps(body, ensure_ascii=False).encode('utf-8')
        elif isinstance(body, str):
            body = body.encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def body(self):
        n = int(self.headers.get('Content-Length') or 0)
        return json.loads(self.rfile.read(n) or b'{}') if n else {}

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        q = urllib.parse.parse_qs(u.query)
        try:
            if u.path in ('/', '/index.html'):
                return self.send(200, open(os.path.join(HERE, 'index.html'), 'rb').read(), 'text/html; charset=utf-8')
            if u.path == '/api/books':
                return self.send(200, books())
            if u.path == '/api/options':
                return self.send(200, {'glossaries': glossaries(), 'models': models(), 'default_model': DEFAULT_MODEL})
            if u.path == '/api/ping':
                return self.send(200, {'ok': True})
            if u.path == '/api/status':
                return self.send(200, system_status())
            if u.path == '/api/jobs':
                ext = [public(j, True) for j in external_runs()]
                with lock:
                    return self.send(200, ext + [public(j, j['status'] == 'running') for j in reversed(jobs)])
            m = re.fullmatch(r'/api/jobs/([\w\-]+)/log', u.path)
            if m:
                j = find(m.group(1))
                if j and j.get('external'):
                    return self.send(200, 'Перевод запущен командой: лог идёт в окне PowerShell, где её запустили.',
                                     'text/plain; charset=utf-8')
                return self.send(200, tail(j['log'], int(q.get('bytes', ['30000'])[0])) if j else '', 'text/plain; charset=utf-8')
            m = re.fullmatch(r'/api/jobs/([\w\-]+)/untranslated', u.path)
            if m:
                return self.send(200, untranslated(m.group(1)))
            m = re.fullmatch(r'/api/jobs/([\w\-]+)/pairs', u.path)
            if m:
                return self.send(200, pairs(m.group(1), int(q.get('offset', ['0'])[0]), min(int(q.get('limit', ['50'])[0]), 200),
                                            q.get('bad', ['0'])[0] == '1'))
            m = re.fullmatch(r'/api/detect/([^/]+)', u.path)
            if m:
                return self.send(200, detect(urllib.parse.unquote(m.group(1))))
            m = re.fullmatch(r'/api/view/([^/]+)', u.path)
            if m:
                return self.send(200, view_info(urllib.parse.unquote(m.group(1))))
            m = re.fullmatch(r'/epub/(en|ru)/([^/]+)/(.+)', u.path)
            if m:
                data = epub_file(m.group(1), urllib.parse.unquote(m.group(2)), urllib.parse.unquote(m.group(3)))
                if data is None:
                    return self.send(404, {'error': 'нет такого файла'})
                ext = os.path.splitext(m.group(3))[1].lower()
                ctype = {'.xhtml': 'text/html; charset=utf-8', '.html': 'text/html; charset=utf-8', '.htm': 'text/html; charset=utf-8',
                         '.css': 'text/css; charset=utf-8', '.png': 'image/png', '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg',
                         '.gif': 'image/gif', '.svg': 'image/svg+xml', '.webp': 'image/webp'}.get(ext, 'application/octet-stream')
                return self.send(200, data, ctype, {'Content-Security-Policy': "script-src 'none'"})
            if u.path.startswith('/files/'):
                return self.file(urllib.parse.unquote(u.path[len('/files/'):]))
            return self.send(404, {'error': 'нет такого адреса'})
        except ValueError as e:
            return self.send(400, {'error': str(e)})
        except Exception as e:
            return self.send(500, {'error': '%s: %s' % (type(e).__name__, e)})

    def do_POST(self):
        u = urllib.parse.urlparse(self.path)
        try:
            if u.path == '/api/jobs':
                return self.send(200, public(new_job(self.body())))
            m = re.fullmatch(r'/api/jobs/([\w\-]+)/(stop|retry)', u.path)
            if m:
                if m.group(2) == 'stop':
                    stop(m.group(1))
                    return self.send(200, {'ok': True})
                return self.send(200, public(retry(m.group(1))))
            if u.path == '/api/history/clear':
                return self.send(200, {'removed': clear_history()})
            if u.path == '/api/docling/unload':
                http_json(DOCLING + '/v1/clear/converters', timeout=30)
                return self.send(200, {'ok': True})
            return self.send(404, {'error': 'нет такого адреса'})
        except ValueError as e:
            return self.send(400, {'error': str(e)})
        except Exception as e:
            return self.send(500, {'error': '%s: %s' % (type(e).__name__, e)})

    def file(self, rel):
        base = os.path.realpath(BOOKS)
        path = os.path.realpath(os.path.join(base, rel))
        if not path.startswith(base + os.sep) or not os.path.isfile(path) or os.sep + '.gui' + os.sep in path:
            return self.send(404, {'error': 'нет такого файла'})
        ctype = {'.txt': 'text/plain; charset=utf-8', '.epub': 'application/epub+zip',
                 '.pdf': 'application/pdf'}.get(os.path.splitext(path)[1].lower(), 'application/octet-stream')
        size = os.path.getsize(path)
        self.send_response(200)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(size))
        if not ctype.startswith('text/'):
            self.send_header('Content-Disposition', "attachment; filename*=UTF-8''" + urllib.parse.quote(os.path.basename(path)))
        self.end_headers()
        with open(path, 'rb') as f:
            shutil.copyfileobj(f, self.wfile)


def main():
    load()
    threading.Thread(target=worker, daemon=True).start()
    threading.Thread(target=autoreload, daemon=True).start()
    threading.Thread(target=sampler, daemon=True).start()
    threading.Thread(target=probe_ollama, daemon=True).start()
    threading.Thread(target=probe_docling, daemon=True).start()

    def warm():                      # после перезапуска кэши пусты: первый /api/books на большой библиотеке — секунды
        try:
            books()
        except Exception:
            pass
    threading.Thread(target=warm, daemon=True).start()
    srv = ThreadingHTTPServer(('0.0.0.0', PORT), H)
    srv.daemon_threads = True
    print('Интерфейс: http://127.0.0.1:%d  (книги: %s, результаты: %s)' % (PORT, SOURCE, BOOKS), flush=True)

    def bye(*_):                      # docker stop: останавливаем перевод аккуратно, он продолжится повтором
        with lock:
            for jid, pr in list(procs.items()):
                j = find(jid)
                if j:
                    j['_stop'] = True
                try:
                    os.killpg(pr.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
        sys.exit(0)
    signal.signal(signal.SIGTERM, bye)
    srv.serve_forever()


if __name__ == '__main__':
    main()
