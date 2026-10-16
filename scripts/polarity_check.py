#!/usr/bin/env python3
"""Возможно перевёрнутый смысл: отрицания и сравнения в оригинале, которых нет в переводе.

Самый частый вид ошибок строения фразы у Rosetta-4B (разметка 13.10, 800 абзацев) — перевёрнутая полярность:
less than → «более чем», not invertible → «невырождена», requires you not make a mistake → «гарантирует отсутствие
ошибок». Проверка сравнивает исходник и перевод из журнала адаптера (.rosetta_log.jsonl):
  - отрицаний в переводе меньше, чем в оригинале (not/no/never/n't/without/neither/nor/cannot/none ↔ не/нет/ни/без/
    никогда/нельзя/ни-слова);
  - сравнение в оригинале (less/fewer/lower/smaller than, more/greater/larger/higher than, at least, at most)
    без соответствующего слова в переводе.
На разметке 13.10: 16 абзацев из 800 (2%), в 4 — настоящая ошибка смысла (при 5% ошибок в среднем). Это подсказка
«посмотреть», а не ошибка: перевод без «не» бывает верным («cannot be empty» → «должен быть заполнен»).

  python polarity_check.py .rosetta_log.jsonl [--json .polarity.json]
"""
import argparse, json, re

MARK = re.compile(r'⟦[^⟧]*⟧')
EN_NEG = re.compile(r"\b(not|no|never|without|neither|nor|cannot|none)\b|n't\b", re.I)
RU_NEG = re.compile(r'(?<![а-яё])(не|нет|ни|без|никогда|нельзя|ничего|ничто|никто|никакой|никакого|никакие|никаких|нигде|ниоткуда)(?![а-яё])')
CMP = [
    (re.compile(r'\b(less|fewer|lower|smaller) than\b', re.I), re.compile(r'(мен(ее|ьше)|ниже|меньш)', re.I), 'less than'),
    (re.compile(r'\b(more|greater|larger|higher) than\b', re.I), re.compile(r'(бол(ее|ьше)|выше|больш|превыша)', re.I), 'more than'),
    (re.compile(r'\bat least\b', re.I), re.compile(r'(как минимум|по крайней мере|не менее|хотя бы|минимум)', re.I), 'at least'),
    (re.compile(r'\bat most\b', re.I), re.compile(r'(не более|максимум|самое большее|не больше)', re.I), 'at most'),
]


def check(src, out):
    s, o = MARK.sub(' ', src), MARK.sub(' ', out)
    why = []
    ne, nr = len(EN_NEG.findall(s)), len(RU_NEG.findall(o.lower()))
    if nr < ne:
        why.append('отрицаний %d → %d' % (ne, nr))
    for en, ru, name in CMP:
        if en.search(s) and not ru.search(o):
            why.append('%s без соответствия' % name)
    return why


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('log')
    ap.add_argument('--json')
    a = ap.parse_args()
    L = [json.loads(l) for l in open(a.log, encoding='utf-8') if l.strip()]
    hits = []
    for i, r in enumerate(L):
        why = check(r.get('src', ''), r.get('out', ''))
        if why:
            hits.append({'n': i, 'why': why, 'src': r['src'], 'out': r['out']})
    print('\nВозможно перевёрнутый смысл (отрицания и сравнения не сошлись): абзацев %d из %d' % (len(hits), len(L)))
    for h in hits[:15]:
        print('  №%d: %s' % (h['n'], '; '.join(h['why'])))
    if len(hits) > 15:
        print('  … ещё %d (подробно — .polarity.json)' % (len(hits) - 15))
    if a.json:
        with open(a.json, 'w', encoding='utf-8') as f:
            json.dump(hits, f, ensure_ascii=False, indent=1)


if __name__ == '__main__':
    main()
