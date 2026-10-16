#!/usr/bin/env python3
"""Заголовки через настоящий адаптер Rosetta (проверка правок 13.10 на модели, 14.10).

Берёт заголовки прошлого теста (headings_c.jsonl от heading_topic_test.py: там вариант C — тот же Context по теме
книги, temperature 0, но старые подсказки) и переводит их ещё раз через rosetta_proxy.py — со всеми его правками:
заголовок из одного сокращения не переводится, автоподсказка «X -> X» для сокращений, подсказки в заголовках
с Заглавными Словами, «⟦var⟧-means». Глоссарий книги собирается build_glossary.py, как в epub.sh; bbm-поиск
терминов повторён упрощённо (целым словом без учёта регистра). Книгу не переводит.

  python replay_headings.py /books/geron_ch10_g2/headings_c.jsonl --out /books/geron_ch10_g2/headings_r.jsonl
         [--map Spring=programming,web] [--domains ml,math] [--model rosetta-ru]
"""
import argparse, json, os, re, subprocess, sys, time, urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.path.dirname(HERE)
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def post(url, body):
    req = urllib.request.Request(url, data=json.dumps(body).encode(), headers={'Content-Type': 'application/json'})
    with OPENER.open(req, timeout=600) as r:
        return json.loads(r.read())


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('headings')
    ap.add_argument('--out', required=True)
    ap.add_argument('--domains', default='ml,math')
    ap.add_argument('--map', action='append', default=[])
    ap.add_argument('--model', default='rosetta-ru')
    ap.add_argument('--target', default='http://ollama:11434')
    ap.add_argument('--config', default='/config')
    ap.add_argument('--tmp', default='/tmp/replay')
    a = ap.parse_args()
    rules = [m.split('=', 1) for m in (a.map or ['Spring=programming,web'])]
    items = [json.loads(l) for l in open(a.headings, encoding='utf-8') if l.strip()]
    for it in items:
        it['dom'] = next((d for part, d in rules if part in it['log']), a.domains)
    os.makedirs(a.tmp, exist_ok=True)
    res = {}
    t0 = time.time()
    for g, dom in enumerate(sorted({it['dom'] for it in items})):
        group = [k for k, it in enumerate(items) if it['dom'] == dom]
        gl, log = os.path.join(a.tmp, 'gl%d.txt' % g), os.path.join(a.tmp, 'log%d.jsonl' % g)
        subprocess.run([sys.executable, os.path.join(SCRIPTS, 'build_glossary.py'), '--format', 'bbm',
                        '--dir', os.path.join(a.config, 'glossary'), '--domains', dom, '--out', gl], check=True)
        terms = []
        for line in open(gl, encoding='utf-8'):
            if ' → ' in line:
                s, t = line.rstrip('\n').split(' → ', 1)
                terms.append((s, t, re.compile(r'(?<![\w-])%s(?![\w-])' % re.escape(s.lower()))))
        if os.path.exists(log):
            os.remove(log)
        port = 8190 + g
        empty = os.path.join(a.tmp, 'senses_empty.json')     # карты смыслов нет: слова default none не подсказываются,
        with open(empty, 'w') as f:                           # как в конвейере для абзаца без решения
            f.write('{}')
        proc = subprocess.Popen([sys.executable, os.path.join(SCRIPTS, 'rosetta_proxy.py'), '--listen', '127.0.0.1:%d' % port,
                                 '--target', a.target, '--log', log, '--glossary-file', gl, '--domains', dom,
                                 '--senses', empty, '--senses-rules', os.path.join(a.config, 'senses.csv')])
        try:
            for _ in range(30):
                try:
                    OPENER.open('http://127.0.0.1:%d/api/tags' % port, timeout=2)
                    break
                except Exception:
                    time.sleep(1)
            for n, k in enumerate(group):
                src = items[k]['src']
                hit = [(s, t) for s, t, rx in terms if rx.search(src.lower())]
                gloss = ('<glossary>\n' + '\n'.join('%s → %s' % x for x in hit) + '\n</glossary>\n') if hit else ''
                body = {'model': a.model, 'temperature': 0, 'messages': [{'role': 'user', 'content':
                        gloss + 'Please help me to translate,`%s` to Russian, please return only translated content' % src}]}
                post('http://127.0.0.1:%d/v1/chat/completions' % port, body)
                if (n + 1) % 50 == 0:
                    print('  %s: %d/%d, %.1f с/заголовок' % (dom, n + 1, len(group), (time.time() - t0) / (len(res) + n + 1)), flush=True)
        finally:
            proc.terminate()
            proc.wait()
        recs = [json.loads(l) for l in open(log, encoding='utf-8') if l.strip()]
        for k, r in zip(group, recs):
            res[k] = r
    changed = flagged = 0
    with open(a.out, 'w', encoding='utf-8') as f:
        for k, it in enumerate(items):
            r = res[k]
            new_terms = [t[0] for t in r['terms']]
            old_terms = [t[0] for t in it.get('terms', [])]
            why = []
            if r.get('as_is'):
                why.append('как в оригинале')
            if r.get('auto_keep'):
                why.append('сокращения: ' + ', '.join(r['auto_keep']))
            if r.get('var_terms'):
                why.append('термин с переменной')
            back = [t for t in new_terms if t not in old_terms and not t.isupper() and t not in (r.get('var_terms') or [])]
            if back:
                why.append('новые подсказки: ' + ', '.join(back))
            changed += r['out'] != it['c']
            flagged += bool(why)
            f.write(json.dumps({'log': it['log'], 'n': it['n'], 'src': it['src'], 'old': it['c'], 'new': r['out'],
                                'why': why, 'terms': r['terms']}, ensure_ascii=False) + '\n')
    print('Готово: заголовков %d, перевод изменился у %d, правки адаптера/глоссария затронули %d → %s'
          % (len(items), changed, flagged, a.out))


if __name__ == '__main__':
    main()
