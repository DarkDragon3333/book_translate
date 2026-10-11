#!/usr/bin/env python3
"""Поиск терминов, которых нет в глоссарии: по английскому тексту книг (до перевода).

Источники текста: .txt (корпус books/.corpus), .epub (только проза), журнал адаптера .jsonl (поле src).
Каждый файл — отдельная книга: для каждого кандидата считается частота и в скольких книгах он встречается.
Глоссарий: все config/glossary/*.csv и config/senses.csv — то, что уже есть, не выводится.

Три вида кандидатов:
  rare   — слово, редкое в обычном английском (classpath, autowiring, camelCase, in-memory);
  phrase — устойчивое сочетание 2–3 слов (starter dependency, container image, integration flow);
  key    — обычное слово, которое в книгах встречается намного чаще, чем в жизни (bean, pod, listing):
           часто многозначное, кандидат скорее в senses.csv, чем в глоссарий.
Частота в обычном английском — wordfreq (en_zipf.tsv.gz рядом со скриптом).

Запуск:
  python term_gaps.py ВХОД... --glossary /config/glossary --senses /config/senses.csv --out кандидаты.csv
"""
import argparse, collections, csv, gzip, json, math, os, re, sys, zipfile
from html.parser import HTMLParser

STOP = set("""a an the and or nor but if then else of to in on at by for with from into onto over under about above
below between through during before after up down out off again further once here there when where why how all any
both each few more most other some such no not only own same so than too very can will just should now is are was were
be been being have has had do does did this that these those it its it's they them their we our you your i my me he
she his her him as also may might must would could shall which who whom whose what one two three new use used using
like via per vs etc e.g i.e let let's see shown show shows following next previous first second last chapter section
figure listing table example examples way ways thing things lot lots bit kind sort part parts case cases time times""".split())
CODEWORDS = set("public private protected void import throws static_void println sout args".split())
TOKEN = re.compile(r"[A-Za-z][A-Za-z0-9]*(?:[-'’][A-Za-z0-9]+)*(?:[+#]+)?")
SENT = re.compile(r"(?<=[.!?])\s+(?=[A-Z⟦\"“(])")
MARK = re.compile(r"⟦[^⟧]*⟧")
# адреса, файлы и пути: application.yml, mng.bz/abc, src/main/java, https://… — это код, не термины
CODEISH = re.compile(r"\b(?:https?://|www\.)\S+|\S*[A-Za-z0-9]\.[A-Za-z]\S*|\S+/\S+|\S+@\S+")

class Prose(HTMLParser):
    SKIP = {"pre", "code", "script", "style", "var", "math", "sup", "sub"}
    def __init__(self):
        super().__init__(); self.depth = 0; self.out = []
    def handle_starttag(self, t, a):
        if t in self.SKIP: self.depth += 1
        if t in ("p", "li", "h1", "h2", "h3", "h4", "dd", "dt", "td", "br", "div"): self.out.append("\n")
    def handle_endtag(self, t):
        if t in self.SKIP and self.depth: self.depth -= 1
    def handle_data(self, d):
        if not self.depth: self.out.append(d)

def read_book(path):
    if path.endswith(".jsonl"):
        out = []
        for l in open(path, encoding="utf-8"):
            try:
                s = json.loads(l).get("src")
            except ValueError:
                continue
            if isinstance(s, str): out.append(s)
        return "\n".join(out)
    if path.endswith(".epub"):
        z, parts = zipfile.ZipFile(path), []
        for n in sorted(z.namelist()):
            if n.endswith((".xhtml", ".html", ".htm")):
                p = Prose(); p.feed(z.read(n).decode("utf-8", "ignore")); parts.append("".join(p.out))
        return "\n".join(parts)
    return open(path, encoding="utf-8", errors="ignore").read()

def norm(w):
    w = w.replace("’", "'").lower()
    return w[:-2] if w.endswith("'s") else w

ZIPF = {}


def lemma(w):
    """Грубое ед. число для сравнения и счёта (classes → class, dependencies → dependency).
    Окончание снимается, только если без него получается известное слово (this, focus, status не трогаются)."""
    if w in STOP:
        return w
    for cond, cut in ((len(w) > 4 and w.endswith("ies"), w[:-3] + "y"),
                      (len(w) > 4 and re.search(r"(ss|sh|ch|x|z)es$", w) is not None, w[:-2]),
                      (len(w) > 3 and w.endswith("s") and not w.endswith(("ss", "us", "is")), w[:-1])):
        if cond:
            # perhaps → «perhap», series → «sery» тоже есть в словаре частот (как мусор) — снимаем окончание,
            # только если форма без него не намного реже исходной
            if w not in ZIPF or ZIPF.get(cut, 0) >= ZIPF[w] - 0.7:
                return cut
            return w
    return w


def load_glossary(gdir, senses):
    have = set()
    for f in sorted(os.listdir(gdir)):
        if f.endswith(".csv"):
            for row in csv.reader(open(os.path.join(gdir, f), encoding="utf-8")):
                if row and not row[0].startswith("#") and row[0] != "source":
                    have.add(" ".join(lemma(norm(w)) for w in row[0].split()))
    if senses and os.path.exists(senses):
        for row in csv.reader(open(senses, encoding="utf-8")):
            if row and not row[0].startswith("#") and row[0] != "word":
                have.add(lemma(norm(row[0])))
    return have

def load_zipf(path):
    z = {}
    with gzip.open(path, "rt", encoding="utf-8") as f:
        for l in f:
            if l.startswith("#"): continue
            w, v = l.rstrip("\n").split("\t"); z[w] = float(v)
    return z

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("inputs", nargs="+", help="файлы или папки (.txt .epub .jsonl)")
    ap.add_argument("--glossary", default="/config/glossary")
    ap.add_argument("--senses", default="/config/senses.csv")
    ap.add_argument("--zipf", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "en_zipf.tsv.gz"))
    ap.add_argument("--min-count", type=int, default=4)
    ap.add_argument("--top", type=int, default=300, help="сколько кандидатов каждого вида выводить")
    ap.add_argument("--out", required=True)
    a = ap.parse_args()

    files = []
    for p in a.inputs:
        if os.path.isdir(p):
            files += [os.path.join(p, f) for f in sorted(os.listdir(p)) if f.endswith((".txt", ".epub", ".jsonl"))]
        else:
            files.append(p)
    zipf = load_zipf(a.zipf)
    ZIPF.update(zipf)
    have = load_glossary(a.glossary, a.senses)
    c1, c2, df, case = collections.Counter(), collections.Counter(), collections.defaultdict(set), \
        collections.defaultdict(collections.Counter)
    total = 0

    def chunks(path):
        """Куски предложений без пунктуации внутри: (предложение, токены, нормализованные слова)."""
        text = CODEISH.sub(" | ", MARK.sub(" | ", read_book(path)))
        for para in text.split("\n"):
            for s in SENT.split(para):
                for chunk in re.split(r"[,;:()\[\]{}\"“”«»—–|]|\s-\s", s):
                    toks = TOKEN.findall(chunk)
                    if toks:
                        yield s, toks, [lemma(norm(t)) for t in toks]

    def ngrams(words):
        for n in (2, 3):
            for i in range(len(words) - n + 1):
                g = words[i:i + n]
                if g[0] in STOP or g[-1] in STOP or any(len(x) < 2 for x in g): continue
                if n == 3 and g[1] in STOP and g[1] != "of": continue
                yield " ".join(g)

    for bi, path in enumerate(files):
        local = collections.Counter()
        for s, toks, words in chunks(path):
            total += len(words)
            for t, w in zip(toks, words):
                c1[w] += 1; df[w].add(bi); case[w][t] += 1
            local.update(ngrams(words))
        for k, n in local.items():          # в общий счёт — только сочетания, повторённые в книге (экономия памяти)
            if n >= 2:
                c2[k] += n; df[k].add(bi)
        print(f"  {os.path.basename(path)}: всего слов {total:,}, сочетаний {len(c2):,}", file=sys.stderr, flush=True)

    pm = lambda n: n / total * 1e6                     # на миллион слов корпуса
    gen_pm = lambda w: 10 ** (zipf.get(w, 0.0) - 3)    # на миллион слов обычного английского
    covered = lambda k: " ".join(lemma(x) for x in k.split()) in have
    rows = []
    for w, n in c1.items():
        if n < a.min_count or w in STOP or covered(w) or len(w) < 3 or w.isdigit(): continue
        orig = case[w].most_common(1)[0][0]
        caps = sum(v for t, v in case[w].items() if t[0].isupper()) / n
        z = zipf.get(w)
        parts = [p for p in re.split(r"[-']", w) if p]
        if z is None and len(parts) > 1:
            # составное через дефис: wordfreq делит его на части; если все части обычные — это не жаргон
            pz = [zipf.get(p, 0.0) for p in parts]
            kind = "hyphen" if min(pz) >= 3.5 else "rare"
            score = math.log(n + 1) * (1 + len(df[w])) * (3.5 - min(pz) / 2)
            rows.append((kind, orig, n, len(df[w]), round(min(pz), 2), score, []))
            continue
        if (z is None or z < 3.0):
            kind = "code?" if re.search(r"[a-z][A-Z]|^[A-Z][a-z]+[A-Z]", orig) and orig != "camelCase" else \
                   ("name?" if caps > 0.9 else "rare")
            score = math.log(n + 1) * (1 + len(df[w])) * (3.5 - (z or 0.5))
        else:
            key = math.log10(pm(n)) - math.log10(gen_pm(w))
            if key < 1.0 or n < 2 * a.min_count: continue
            kind, score = "key", key * math.log(n + 1) * (1 + len(df[w]))
        rows.append((kind, orig, n, len(df[w]), z, score, []))
    for k, n in c2.items():
        if n < a.min_count or covered(k): continue
        parts = k.split()
        # обрывки кода и битый текст PDF: «public void», «pu lic», «throw ioexception», «void userdeletedeventhandler»
        if any(p in CODEWORDS for p in parts): continue
        if any(len(p) < 3 and zipf.get(p, 0) < 3 for p in parts): continue
        if any(p not in zipf and "-" not in p and len(p) > 10 for p in parts): continue
        stick = n / min(c1[p] for p in parts)
        if stick < 0.08: continue
        spec = sum(max(0.0, math.log10(pm(c1[p]) + 1e-9) - math.log10(gen_pm(p) + 1e-3)) for p in parts) / len(parts)
        if spec < 0.4: continue
        caps = all(any(t[0].isupper() for t in case[p]) and case[p].most_common(1)[0][0][0].isupper() for p in parts)
        kind = "name?" if caps else "phrase"
        rows.append((kind, k, n, len(df[k]), None, math.log(n + 1) * (1 + len(df[k])) * stick * (1 + spec), []))
    # сочетание, целиком входящее в более длинное с той же частотой, — выбрасываем короткое
    longer = collections.defaultdict(int)
    for r in rows:
        if r[0] in ("phrase", "name?") and len(r[1].split()) == 3:
            longer[" ".join(r[1].split()[:2])] = max(longer[" ".join(r[1].split()[:2])], r[2])
            longer[" ".join(r[1].split()[1:])] = max(longer[" ".join(r[1].split()[1:])], r[2])
    rows = [r for r in rows if not (len(r[1].split()) == 2 and longer.get(r[1].lower(), 0) >= 0.8 * r[2])]

    order = {"rare": 0, "phrase": 1, "hyphen": 2, "key": 3, "code?": 4, "name?": 5}
    out = []
    for kind in order:
        sel = sorted((r for r in rows if r[0] == kind), key=lambda r: -r[5])[:a.top]
        out += sel
    # второй проход: по два примера предложений для выбранных кандидатов (из разных книг, если можно)
    want = {(r[1].lower() if r[0] in ("phrase", "name?") else lemma(norm(r[1]))): r[6] for r in out}
    for path in files:
        seen = set()
        for s, toks, words in chunks(path):
            if len(s) > 260: continue
            keys = set(words) | set(ngrams(words))
            for k in keys & want.keys():
                if len(want[k]) < 2 and k not in seen and s not in want[k]:
                    want[k].append(s.strip()); seen.add(k)
    with open(a.out, "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["kind", "term", "count", "books", "zipf", "score", "example1", "example2"])
        for kind, t, n, d, z, sc, e in out:
            w.writerow([kind, t, n, d, "" if z is None else z, round(sc, 1)] + (e + ["", ""])[:2])
    by = collections.Counter(r[0] for r in out)
    print(f"Книг: {len(files)}, слов: {total:,}. Кандидатов: " + ", ".join(f"{k} {by[k]}" for k in order) +
          f". Файл: {a.out}", file=sys.stderr)

if __name__ == "__main__":
    main()
