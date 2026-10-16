#!/usr/bin/env python3
"""Проба EEVE-Rosetta-7B (rosetta7-ru): почему модель не заканчивает ответ.

1) Токенизация служебных меток: raw-запрос из одной метки; если prompt_eval_count = 2 (BOS + метка),
   метка — один служебный токен; если больше — Ollama режет её на куски текста.
2) Короткие запросы как у адаптера (system + {"text": …}) с ограничением 120 токенов:
   done_reason=length — модель не поставила конец ответа; печатается начало ответа.
3) То же через raw-шаблон вручную и с repeat_penalty 1.1.

Запуск: docker.exe compose run --rm --no-deps --entrypoint python translate /scripts/tools/rosetta7_probe.py [модель ...]
"""
import json, sys, urllib.request

T = 'http://ollama:11434'
op = urllib.request.build_opener(urllib.request.ProxyHandler({}))
models = sys.argv[1:] or ['rosetta7-ru', 'rosetta-ru']
SYS = ("Translate the user's text to Russian.\nContext: A technical book about software development.\n"
       "Tone: Clear, natural technical prose, as in a professionally edited book.\n"
       "Output format: JSON\nProvide the final translation immediately without any other text.")
TEXTS = ['This chapter covers', 'Building Spring applications as container images', 'Weighing deployment options']


def post(path, body):
    req = urllib.request.Request(T + path, data=json.dumps(body).encode(), method='POST',
                                 headers={'Content-Type': 'application/json'})
    with op.open(req, timeout=600) as r:
        return json.loads(r.read())


def show(tag, r, text):
    print('  %-28s done=%-6s gen=%-4s %r' % (tag, r.get('done_reason'), r.get('eval_count'), text[:110]))


for m in models:
    print('=== модель', m)
    for mark in ['<start_of_turn>', '<end_of_turn>', 'hello']:
        r = post('/api/generate', {'model': m, 'prompt': mark, 'raw': True, 'stream': False,
                                   'options': {'num_predict': 1, 'temperature': 0}})
        print('  токенов в запросе %-18r = %s' % (mark, r.get('prompt_eval_count')))
    for t in TEXTS:
        user = json.dumps({'text': t}, ensure_ascii=False)
        r = post('/api/chat', {'model': m, 'stream': False, 'options': {'temperature': 0.2, 'num_predict': 120},
                               'messages': [{'role': 'system', 'content': SYS}, {'role': 'user', 'content': user}]})
        show('chat t=0.2', r, r['message']['content'])
        r = post('/api/chat', {'model': m, 'stream': False,
                               'options': {'temperature': 0.2, 'num_predict': 120, 'repeat_penalty': 1.1},
                               'messages': [{'role': 'system', 'content': SYS}, {'role': 'user', 'content': user}]})
        show('chat t=0.2 rp=1.1', r, r['message']['content'])
        raw = ('<start_of_turn>instruction\n%s<end_of_turn>\n<start_of_turn>source\n%s<end_of_turn>\n'
               '<start_of_turn>translation\n' % (SYS, user))
        r = post('/api/generate', {'model': m, 'prompt': raw, 'raw': True, 'stream': False,
                                   'options': {'temperature': 0, 'num_predict': 120}})
        show('raw t=0', r, r['response'])
