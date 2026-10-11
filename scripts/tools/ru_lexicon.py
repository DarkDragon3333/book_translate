#!/usr/bin/env python3
"""Словарь русских слов из корпуса изданных книг — для ru_check.py (проверка несуществующих слов в переводе).
Берёт тексты books/.corpus/*.txt (их делает corpus_text.py), оставляет русские книги (больше половины букв —
кириллица) и слова, встретившиеся в корпусе 3+ раз (обрывки переносов и опечатки издательств отсеиваются).
В словаре только отдельные слова, текста книг в нём нет. Файл личный — в репозиторий не класть.

  docker.exe compose run --rm --no-deps -v C:\\book_translate\\config:/cfg --entrypoint python pdf-epub /scripts/tools/ru_lexicon.py /books/.corpus /cfg/ru_corpus_words.txt.gz
"""
import glob, gzip, os, re, sys
from collections import Counter

WORD = re.compile(r'[А-Яа-яЁё]+(?:-[А-Яа-яЁё]+)*')


def main():
    src, out = sys.argv[1], sys.argv[2]
    total, books = Counter(), 0
    for f in sorted(glob.glob(os.path.join(src, '*.txt'))):
        text = open(f, encoding='utf-8', errors='ignore').read()
        cyr = len(re.findall('[А-Яа-яЁё]', text))
        lat = len(re.findall('[A-Za-z]', text))
        if cyr < 20000 or cyr < lat:
            continue
        books += 1
        total.update(w.lower().replace('ё', 'е') for w in WORD.findall(text))
    words = sorted(w for w, c in total.items() if c >= 3 and len(w) >= 2)
    with gzip.open(out, 'wt', encoding='utf-8') as fh:
        fh.write('\n'.join(words))
    print('Русских книг: %d, слов в словаре: %d → %s' % (books, len(words), out))


if __name__ == '__main__':
    main()
