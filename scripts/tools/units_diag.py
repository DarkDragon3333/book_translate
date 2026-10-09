#!/usr/bin/env python3
"""Почему интерфейс насчитал 0 абзацев: по каждому файлу work.epub — пропущен ли и почему, сколько абзацев, ошибка.
Текст книги не печатает. Запуск: docker.exe exec book-gui python /scripts/tools/units_diag.py ocp
(аргумент — начало имени папки книги в books)."""
import glob, os, re, sys, traceback, zipfile
sys.path.insert(0, '/scripts/gui')
import server as S

pref = sys.argv[1] if len(sys.argv) > 1 else ''
for w in sorted(glob.glob('/books/%s*' % pref)):
    if not os.path.isdir(w):
        continue
    print('== папка', w)
    for n in sorted(os.listdir(w)):
        p = os.path.join(w, n)
        print('   %-45s %12s' % (n, os.path.getsize(p) if os.path.isfile(p) else '<dir>'))
    work = os.path.join(w, 'work.epub')
    if not os.path.isfile(work):
        print('   work.epub нет')
        continue
    try:
        z = zipfile.ZipFile(work)
    except Exception as e:
        print('   work.epub не открывается:', e)
        continue
    names = z.namelist()
    print('   файлов в EPUB: %d, html/xhtml: %d' % (len(names), sum(f.endswith(('.xhtml', '.html', '.htm')) for f in names)))
    exts = {}
    for f in names:
        e = os.path.splitext(f)[1].lower()
        exts[e] = exts.get(e, 0) + 1
    print('   расширения:', exts)
    total = 0
    for f in names:
        if not f.endswith(('.xhtml', '.html', '.htm')):
            continue
        why = ''
        if os.path.basename(f).startswith('nav'):
            why = 'имя nav*'
        try:
            t = z.read(f).decode('utf-8', 'replace')
            m = re.search(r'data-type="index"|epub:type="[^"]*\bindex\b|<nav\b', t)
            if m and not why:
                why = 'найдено: ' + m.group(0)[:40]
            u = S._Units()
            u.feed(t)
            total += 0 if why else u.units
            print('   %-50s абзацев %5d %s' % (f[-50:], u.units, ('ПРОПУЩЕН (%s)' % why) if why else ''))
        except Exception:
            print('   %-50s ОШИБКА: %s' % (f[-50:], traceback.format_exc().strip().splitlines()[-1]))
    print('   итого (как в интерфейсе): %d' % total)
    try:
        print('   count_units():', S.count_units(work, None))
    except Exception:
        print('   count_units() ОШИБКА:', traceback.format_exc().strip().splitlines()[-1])
