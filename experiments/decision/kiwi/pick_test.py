r"""Выбор лучшей версии на отмеченных абзацах: Rosetta (из журнала) или translategemma.
16 настоящих ошибок (версия translategemma — из retry_results.jsonl) и 37 ложных тревог (translategemma переводит сейчас).
Обе версии оцениваются CometKiwi. Запуск — см. чат. Пишет /books/.dm/pick_results.jsonl.
"""
import json, re, sys, time, urllib.request
from comet import download_model, load_from_checkpoint
import torch
import re
ABBR_EN = r"(?<!\be\.g)(?<!\bi\.e)(?<!\betc)(?<!\bvs)(?<!\bMr)(?<!\bDr)(?<!\bNo)(?<!\bFig)"
EN = re.compile(ABBR_EN + r"(?<=[.!?])[\"”’)]?\s+(?=[A-Z⟦\"“(])")
RU = re.compile(r"(?<!\bт\.е)(?<!\bт\.д)(?<!\bт\.п)(?<!\bрис)(?<!\bсм)(?<=[.!?…])[»\")]?\s+(?=[А-ЯЁA-Z⟦«\"(])")
def split_en(t): return [s for s in EN.split(t.strip()) if s.strip()]
def split_ru(t): return [s for s in RU.split(t.strip()) if s.strip()]

ERR = [163, 431, 605, 854, 1368, 1393, 1701, 1728, 1815, 1938, 2018, 2119, 2196, 2558, 3330, 3495]
FA = [37, 168, 214, 218, 417, 463, 573, 584, 824, 991, 1073, 1328, 1550, 1579, 1605, 1645, 1819, 1846, 1847, 2000, 2100, 2177, 2296, 2301, 2307, 2364, 2429, 2567, 2568, 2626, 2771, 2914, 3042, 3226, 3227, 3251, 3252]
URL = "http://ollama:11434/api/chat"

def clean(t):
    t = t.strip()
    for a, b in (("`", "`"), ("«", "»"), ('"', '"')):
        if len(t) > 2 and t.startswith(a) and t.endswith(b):
            t = t[1:-1].strip()
    return t

def gemma(src, terms):
    g = ("<glossary>\n" + "\n".join("%s → %s" % (a, b) for a, b in terms) + "\n</glossary>\n") if terms else ""
    ask = g + "Please help me to translate,`%s` to Russian, please return only translated content not include the origin text" % src
    body = json.dumps({"model": "translategemma-gpu", "stream": False, "options": {"temperature": 0.2},
                       "messages": [{"role": "user", "content": ask}]}, ensure_ascii=False).encode()
    req = urllib.request.Request(URL, data=body, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=900) as r:
        return json.load(r)["message"]["content"]

def main():
    torch.set_num_threads(6)
    lines = open(sys.argv[1], encoding="utf-8").read().splitlines()
    retry = {json.loads(l)["line"]: json.loads(l) for l in open(sys.argv[2], encoding="utf-8")}
    rows, t0 = [], time.time()
    for ln in ERR + FA:
        j = json.loads(lines[ln])
        g = retry[ln]["gemma"] if ln in ERR else gemma(j["src"], j.get("terms") or [])
        rows.append({"line": ln, "kind": "err" if ln in ERR else "fa", "src": j["src"], "ros": j["out"], "gem": clean(g)})
    print(f"translategemma: {len(FA)} абзацев за {time.time()-t0:.0f} с", flush=True)
    model = load_from_checkpoint(download_model("Unbabel/wmt22-cometkiwi-da"))
    items, own = [], []
    for i, r in enumerate(rows):
        for v in ("ros", "gem"):
            items.append({"src": r["src"], "mt": r[v]}); own.append((i, v, -1))
            en, ru = split_en(r["src"]), split_ru(r[v])
            if len(en) == len(ru) and len(en) > 1:
                for e, t in zip(en, ru):
                    items.append({"src": e, "mt": t}); own.append((i, v, 1))
    sc = model.predict(items, batch_size=8, gpus=0, progress_bar=False).scores
    for r in rows:
        for v in ("ros", "gem"):
            r[v + "_par"], r[v + "_sents"] = None, []
    for (i, v, k), s in zip(own, sc):
        if k < 0: rows[i][v + "_par"] = s
        else: rows[i][v + "_sents"].append(s)
    with open("/books/.dm/pick_results.jsonl", "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    sent = lambda r, v: min(r[v + "_sents"]) if r[v + "_sents"] else r[v + "_par"]
    print(f"\nПравило «взять translategemma, если её оценка выше»:")
    for name, f in (("по абзацу", lambda r, v: r[v + "_par"]), ("по худшему предложению", sent)):
        e = sum(f(r, "gem") > f(r, "ros") for r in rows if r["kind"] == "err")
        a = sum(f(r, "gem") > f(r, "ros") for r in rows if r["kind"] == "fa")
        print(f"  {name:24} ошибки: выбрана translategemma {e} из {len(ERR)}; ложные тревоги: {a} из {len(FA)}")
    print(f"Готово за {(time.time()-t0)/60:.1f} мин. Файл: books/.dm/pick_results.jsonl")

if __name__ == "__main__":
    main()
