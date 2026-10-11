#!/usr/bin/env python3
"""Несуществующие русские слова в переводе: признак бессмыслицы модели («ПОМИДОЧНОГО», «сбойт», «зятся»).
Слово считается известным, если оно есть:
  - в частотном словаре русского языка (scripts/tools/ru_words.txt.gz — wordfreq large_ru, CC BY-SA 4.0);
  - в словаре корпуса изданных русских книг (config/ru_corpus_words.txt.gz, собирает tools/ru_lexicon.py);
  - среди переводов глоссария (config/glossary/*.csv);
  - или у него есть известный «родственник» с той же основой (репозиториев → репозиторий).
Не проверяются: код, слова с заглавной в середине предложения (имена), слова короче 4 букв.
Слово, которое модель употребила в 4+ абзацах этой книги, считается жаргоном книги, а не сбоем.

  python ru_check.py перевод.epub --words ru_words.txt.gz [--corpus ru_corpus_words.txt.gz] [--glossary-dir dir]
                     [--only ch01.xhtml,…] [--json подробно.json]
Печатает раздел для отчёта проверки.
"""
import argparse, bisect, glob, gzip, json, os, re, sys, zipfile
from collections import Counter, defaultdict
from bs4 import BeautifulSoup

BLOCKS = ['p', 'li', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'td', 'th', 'dt', 'dd', 'figcaption', 'caption']
WORD = re.compile(r'(?<![\w-])[А-Яа-яЁё]+(?:-[А-Яа-яЁёA-Za-z0-9]+)*')
END = sorted(('иями ями ами ого его ому ему ыми ими ой ей ий ый ая яя ое ее ые ие ов ев ам ям ах ях ом ем ую юю '
              'ть ет ит ут ют ат ят ешь ишь ете ите ла ло ли ал ил ся сь вшись вши ав ив ув ых их ым ою ею '
              'ются ется ится утся атся ятся лся лась лось лись айте яйте ейте уйте йте ьте а я о е ы и у ю ь й л в').split(), key=len, reverse=True)
BOOK_JARGON = 4
DERIV = ('ированн', 'ованн', 'ующ', 'ющ', 'ащ', 'ящ', 'вш', 'енн', 'онн', 'ирова', 'ируе', 'иру')
PREFIXES = ('не', 'пере', 'пред', 'пере', 'под', 'раз', 'рас', 'со', 'с', 'за', 'на', 'от', 'вы', 'пр', 'до', 'об', 'пере', 'пост', 'авто', 'мульти', 'микро', 'меж', 'суб', 'супер', 'анти', 'псевдо')


def norm(w):
    return w.lower().replace('ё', 'е')


class Lexicon:
    def __init__(self, files, glossary_dir=None):
        words = set()
        for f in files:
            if f and os.path.isfile(f):
                with gzip.open(f, 'rt', encoding='utf-8') as fh:
                    words.update(norm(w) for w in fh.read().split('\n') if w)
        if glossary_dir:
            for f in glob.glob(os.path.join(glossary_dir, '*.csv')):
                for line in open(f, encoding='utf-8-sig'):
                    if not line.startswith('#') and ',' in line:
                        words.update(norm(w) for w in WORD.findall(line.split(',')[1]))
        self.words = words
        self.keys = sorted(w for w in words if '-' not in w)

    def stem_known(self, w):
        for e in END + ['']:
            if e and not w.endswith(e):
                continue
            s = w[:len(w) - len(e)]
            if len(s) < 5:
                continue
            i = bisect.bisect_left(self.keys, s)
            while i < len(self.keys) and self.keys[i].startswith(s):
                if len(self.keys[i]) <= len(s) + 5:
                    return True
                i += 1
        return False

    def known(self, w):
        n = norm(w)
        if n in self.words:
            return True
        if '-' in n:
            return all(self.known(p) or not re.search('[а-я]', p) for p in n.split('-') if p)
        if self.stem_known(n):
            return True
        for e in END + ['']:                        # причастия и глаголы: маршрутизирующих → маршрутиз-, авторизационного → авторизаци-
            s = n[:len(n) - len(e)] if e and n.endswith(e) else (n if not e else None)
            if s:
                for d in DERIV:
                    if s.endswith(d) and len(s) - len(d) >= 6 and self.stem_known(s[:len(s) - len(d)]):
                        return True
        for pre in PREFIXES:                       # нереактивный → реактивный, сконфигурирован → конфигурирован
            if n.startswith(pre) and len(n) - len(pre) >= 6 and (n[len(pre):] in self.words or self.stem_known(n[len(pre):])):
                return True
        return False


def paragraphs(path, only=None):
    """(файл, текст) блоков перевода без кода."""
    z = zipfile.ZipFile(path)
    for name in sorted(z.namelist()):
        if not name.lower().endswith(('.xhtml', '.html', '.htm')) or name.endswith('nav.xhtml'):
            continue
        base = os.path.basename(name)
        if only and base not in only:
            continue
        soup = BeautifulSoup(z.read(name), 'html.parser')
        for t in soup.find_all(['pre', 'code', 'kbd', 'samp', 'math', 'script', 'style']):
            t.decompose()
        for b in soup.find_all(BLOCKS):
            if b.find(BLOCKS):                     # берём самые внутренние блоки
                continue
            text = ' '.join(b.get_text(' ').split())
            if re.search('[А-Яа-яЁё]', text):
                yield base, text


def suspects(text, lex):
    out = []
    for m in WORD.finditer(text):
        w = m.group(0)
        if len(w) < 4:
            continue
        before = text[max(0, m.start() - 3):m.start()]
        start = m.start() == 0 or re.search(r'[.!?:»"…]\s*$|^\s*[«"(—–-]?\s*$', before) is not None
        if w[0].isupper() and not w.isupper() and not start:
            continue                                # имя собственное
        if not lex.known(w):
            out.append(w)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('epub')
    ap.add_argument('--words', required=True)
    ap.add_argument('--corpus')
    ap.add_argument('--glossary-dir')
    ap.add_argument('--only')
    ap.add_argument('--json')
    a = ap.parse_args()
    if not os.path.isfile(a.words):
        print('\nНесуществующие русские слова: не проверено — нет словаря %s' % a.words)
        return
    lex = Lexicon([a.words, a.corpus], a.glossary_dir)
    only = {os.path.basename(x) for x in a.only.split(',') if x} if a.only else None
    hits, n_par = [], 0
    for name, text in paragraphs(a.epub, only):
        n_par += 1
        ws = suspects(text, lex)
        if ws:
            hits.append((name, text, ws))
    spread = defaultdict(set)
    for i, (_, _, ws) in enumerate(hits):
        for w in ws:
            spread[norm(w)].add(i)
    jargon = {w for w, s in spread.items() if len(s) >= BOOK_JARGON}
    bad = [(n, t, [w for w in ws if norm(w) not in jargon]) for n, t, ws in hits]
    bad = [b for b in bad if b[2]]
    words = Counter(norm(w) for _, _, ws in bad for w in ws)
    by_file = Counter(n for n, _, _ in bad)
    print('\nНесуществующие русские слова (нет в словаре; возможна бессмыслица): абзацев %d из %d' % (len(bad), n_par))
    if bad:
        for n, c in sorted(by_file.items()):
            print('  %s: %d' % (n, c))
        print('  Слова: ' + ', '.join(w for w, _ in words.most_common(40)) + (' …' if len(words) > 40 else ''))
    if jargon:
        print('  Не считаются (жаргон книги, 4+ абзацев): ' + ', '.join(sorted(jargon)[:20]) + (' …' if len(jargon) > 20 else ''))
    if a.json:
        with open(a.json, 'w', encoding='utf-8') as f:
            json.dump([{'file': n, 'words': ws, 'text': t} for n, t, ws in bad], f, ensure_ascii=False, indent=1)


if __name__ == '__main__':
    main()
