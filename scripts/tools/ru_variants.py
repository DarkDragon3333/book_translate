#!/usr/bin/env python3
"""Как русские издания переводят спорные термины: частота вариантов перевода в русских книгах корпуса.

Вход: файл вариантов (строки «термин | вариант | регулярка», регистр не важен) и папки корпуса
(books/.corpus, *.txt + manifest.json; русские книги — lang=ru в manifest или >50% кириллицы).
Выход: CSV term,variant,count,books — только числа, без текста книг.

  python ru_variants.py variants.txt /books/.corpus --out /books/ru_variants.csv
"""
import argparse, csv, json, os, re, sys

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('variants')
    ap.add_argument('corpus', nargs='+')
    ap.add_argument('--out', required=True)
    a = ap.parse_args()
    var = []
    for l in open(a.variants, encoding='utf-8'):
        if l.startswith('#') or not l.strip():
            continue
        t, v, rx = [x.strip() for x in l.rstrip('\n').split('|', 2)]
        var.append((t, v, re.compile(rx, re.I)))
    books = []
    for d in a.corpus:
        man = {}
        mp = os.path.join(d, 'manifest.json')
        if os.path.exists(mp):
            man = json.load(open(mp, encoding='utf-8-sig'))
        for root, _, files in os.walk(d):
            for f in sorted(files):
                if not f.endswith('.txt'):
                    continue
                text = open(os.path.join(root, f), encoding='utf-8', errors='ignore').read()
                lang = man.get(f[:-4], {}).get('lang')
                if lang is None:
                    letters = re.findall(r'[A-Za-zА-Яа-яЁё]', text[:200000])
                    lang = 'ru' if letters and sum(c > 'z' for c in letters) / len(letters) > 0.5 else 'en'
                if lang == 'ru':
                    books.append((f, text.replace('ё', 'е').replace('Ё', 'Е')))
    print(f'Русских книг: {len(books)}', file=sys.stderr)
    with open(a.out, 'w', encoding='utf-8', newline='') as fo:
        w = csv.writer(fo)
        w.writerow(['term', 'variant', 'count', 'books'])
        for t, v, rx in var:
            n = nb = 0
            for _, text in books:
                k = len(rx.findall(text))
                n += k
                nb += k > 0
            w.writerow([t, v, n, nb])
    print(f'Вариантов: {len(var)}. Файл: {a.out}', file=sys.stderr)

if __name__ == '__main__':
    main()
