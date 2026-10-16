#!/usr/bin/env python3
"""Гибридный режим, проход 2: какие абзацы перевести заново сильной моделью (Rosetta-12B).

Вход — журнал адаптера быстрого прохода (Rosetta-4B). Абзацы отбираются по порядку, пока не наберётся
--share процентов текста книги (по длине оригинала):
  1) порча меток или ответ не JSON (адаптер не справился и с повторами);
  2) несуществующие русские слова (те же правила, что ru_check: словари, 4+ абзацев — жаргон книги);
  3) остальные — по оценке ReMedy-9B, от худшей.
Проверка 13–14.10 на двух свежих главах (18 ошибок смысла 4B, отчёт claude/reports/2026-10-14-remedy-spring11.md):
20% текста → 56% ошибок, 30% → 61%, 40% → 83%.

Перед оценкой модели Ollama выгружаются (видеокарта одна), после — выгружается ReMedy.
Оценки пишутся по ходу в --qe (повторный запуск после обрыва продолжает с места остановки).

  python hybrid_pick.py .hybrid_fast.jsonl --share 40 --qe .hybrid_qe.jsonl --out .hybrid_pick.json
         --words ru_words.txt.gz --corpus ru_corpus_words.txt.gz --glossary-dir /config/glossary
         [--remedy http://remedy:8091] [--ollama http://ollama:11434]
"""
import argparse, json, os, re, sys, time, urllib.request
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ru_check

MARK = re.compile(r'⟦[a-z]+\d+⟧')
opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def log(msg):
    print('[%s]     %s' % (time.strftime('%H:%M:%S'), msg), flush=True)


def http(url, data=None, timeout=600):
    req = urllib.request.Request(url, data=json.dumps(data).encode() if data is not None else None,
                                 headers={'Content-Type': 'application/json'} if data is not None else {})
    with opener.open(req, timeout=timeout) as r:
        return json.loads(r.read() or b'{}')


def unload_ollama(url):
    """Выгрузить все модели Ollama и дождаться, пока видеопамять освободится (keep_alive 0 выгружает не сразу)."""
    try:
        for m in http(url + '/api/ps', timeout=10).get('models', []):
            http(url + '/api/generate', {'model': m['name'], 'keep_alive': 0}, timeout=60)
        for _ in range(60):
            if not http(url + '/api/ps', timeout=10).get('models'):
                return True
            time.sleep(1)
    except Exception as e:
        log('Ollama: не удалось выгрузить модели (%s)' % e)
    return False


def plain(t):
    return MARK.sub(' ', t).replace('⟪', '').replace('⟫', '')


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('log')
    ap.add_argument('--share', type=float, required=True, help='процент текста книги для сильной модели')
    ap.add_argument('--qe', required=True)
    ap.add_argument('--out', required=True)
    ap.add_argument('--words')
    ap.add_argument('--corpus')
    ap.add_argument('--glossary-dir')
    ap.add_argument('--remedy', default=os.environ.get('REMEDY_URL', 'http://remedy:8091'))
    ap.add_argument('--ollama', default=os.environ.get('OLLAMA_URL', 'http://ollama:11434'))
    ap.add_argument('--batch', type=int, default=16)
    a = ap.parse_args()

    recs, seen = [], set()
    for line in open(a.log, encoding='utf-8'):
        if not line.strip():
            continue
        r = json.loads(line)
        if r['src'] in seen or r.get('as_is'):
            continue                            # повтор абзаца или заголовок-название (не переводился)
        seen.add(r['src'])
        recs.append(r)
    total = sum(len(r['src']) for r in recs) or 1

    # 1–2: сбои адаптера и несуществующие слова
    broken = {r['src'] for r in recs if not r.get('marks_ok', True) or not r.get('json', True)}
    ruw = {}
    if a.words and os.path.isfile(a.words):
        lex = ru_check.Lexicon([a.words, a.corpus], a.glossary_dir)
        hits = [(r['src'], ru_check.suspects(plain(r['out']), lex)) for r in recs]
        spread = defaultdict(set)
        for i, (_, ws) in enumerate(hits):
            for w in ws:
                spread[ru_check.norm(w)].add(i)
        jargon = {w for w, s in spread.items() if len(s) >= ru_check.BOOK_JARGON}
        for src, ws in hits:
            ws = [w for w in ws if ru_check.norm(w) not in jargon]
            if ws:
                ruw[src] = ws
    else:
        log('ru_check: нет словаря — отбор только по ReMedy')

    # 3: оценка ReMedy (с продолжением)
    scores = {}
    if os.path.isfile(a.qe):
        for line in open(a.qe, encoding='utf-8'):
            if line.strip():
                q = json.loads(line)
                scores[q['src']] = q['s']
    todo = [r for r in recs if r['src'] not in scores]
    if todo:
        try:
            st = http(a.remedy + '/status', timeout=10)
        except Exception as e:
            sys.exit('ОШИБКА: сервис оценки ReMedy не отвечает (%s). Запусти: docker compose up -d remedy' % e)
        if not st.get('available'):
            sys.exit('ОШИБКА: нет модели ReMedy (%s). Установка — README, раздел «Гибридный режим».' % st.get('model'))
        log('Выгружаю модели Ollama: видеопамять нужна ReMedy')
        if not unload_ollama(a.ollama):
            log('Ollama не выгрузила модели за минуту — продолжаю (ReMedy может не поместиться в видеопамять)')
        log('Оценка ReMedy: %d абзацев (уже оценено %d), ~1 с на абзац' % (len(todo), len(recs) - len(todo)))
        t0, done = time.time(), 0
        try:
            with open(a.qe, 'a', encoding='utf-8') as f:
                for i in range(0, len(todo), a.batch):
                    part = todo[i:i + a.batch]
                    res = http(a.remedy + '/score', {'pairs': [{'src': r['src'], 'mt': r['out']} for r in part]}, timeout=1800)
                    for r, s in zip(part, res['scores']):
                        scores[r['src']] = s
                        f.write(json.dumps({'src': r['src'], 's': s}, ensure_ascii=False) + '\n')
                    f.flush()
                    done += len(part)
                    if done % 400 < a.batch or done == len(todo):
                        rate = (time.time() - t0) / done
                        log('оценено %d из %d, осталось ~%d мин' % (done, len(todo), round((len(todo) - done) * rate / 60)))
        finally:
            try:
                http(a.remedy + '/unload', {}, timeout=60)
            except Exception:
                pass

    # отбор по доле текста
    inf = float('inf')
    sc = lambda r: scores.get(r['src']) if scores.get(r['src']) is not None else inf
    first = sorted([r for r in recs if r['src'] in broken], key=sc) + \
        sorted([r for r in recs if r['src'] in ruw and r['src'] not in broken], key=sc)
    rest = sorted([r for r in recs if r['src'] not in broken and r['src'] not in ruw and scores.get(r['src']) is not None], key=sc)
    limit = a.share / 100 * total
    picked, chars, why = [], 0, {'broken': 0, 'ru_check': 0, 'remedy': 0}
    for r in first + rest:
        if chars >= limit:
            break
        picked.append(r['src'])
        chars += len(r['src'])
        why['broken' if r['src'] in broken else 'ru_check' if r['src'] in ruw else 'remedy'] += 1
    unscored = sum(1 for r in recs if scores.get(r['src']) is None)
    out = {'share': a.share, 'paragraphs': len(recs), 'total_chars': total, 'n': len(picked), 'chars': chars,
           'pct': round(100 * chars / total, 1), 'why': why, 'unscored': unscored, 'picked': picked}
    json.dump(out, open(a.out, 'w', encoding='utf-8'), ensure_ascii=False, indent=1)
    log('Отобрано для сильной модели: %d абзацев из %d (%.1f%% текста; метки/JSON %d, ru_check %d, ReMedy %d)%s'
        % (len(picked), len(recs), out['pct'], why['broken'], why['ru_check'], why['remedy'],
           '; без оценки (длиннее окна ReMedy) %d' % unscored if unscored else ''))


if __name__ == '__main__':
    main()
