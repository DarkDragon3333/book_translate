r"""CometKiwi на журнале Rosetta: скорость на CPU и сравнение с tev1 на размеченных абзацах Spring.
Запуск (из C:\book_translate), см. команду в чате. Пишет /books/.dm/kiwi_results.jsonl.
"""
import argparse, json, time, torch
from comet import download_model, load_from_checkpoint
import re
ABBR_EN = r"(?<!\be\.g)(?<!\bi\.e)(?<!\betc)(?<!\bvs)(?<!\bMr)(?<!\bDr)(?<!\bNo)(?<!\bFig)"
EN = re.compile(ABBR_EN + r"(?<=[.!?])[\"”’)]?\s+(?=[A-Z⟦\"“(])")
RU = re.compile(r"(?<!\bт\.е)(?<!\bт\.д)(?<!\bт\.п)(?<!\bрис)(?<!\bсм)(?<=[.!?…])[»\")]?\s+(?=[А-ЯЁA-Z⟦«\"(])")
def split_en(t): return [s for s in EN.split(t.strip()) if s.strip()]
def split_ru(t): return [s for s in RU.split(t.strip()) if s.strip()]

LAB80 = {"64": "ok", "102": "ok", "148": "ok", "153": "ok", "185": "ok", "191": "minor", "198": "minor", "267": "minor", "315": "ok", "418": "minor", "456": "ok", "457": "minor", "536": "ok", "610": "minor", "651": "ok", "667": "ok", "674": "ok", "706": "ok", "743": "ok", "833": "ok", "934": "minor", "1015": "ok", "1069": "ok", "1083": "minor", "1141": "err", "1197": "ok", "1267": "ok", "1335": "minor", "1347": "ok", "1394": "ok", "1411": "minor", "1468": "ok", "1631": "err", "1646": "ok", "1724": "ok", "1817": "ok", "1875": "err", "1969": "ok", "1971": "minor", "1992": "minor", "2020": "ok", "2025": "err", "2090": "ok", "2116": "ok", "2138": "ok", "2188": "minor", "2241": "err", "2252": "err", "2268": "ok", "2314": "minor", "2376": "ok", "2454": "ok", "2464": "minor", "2480": "ok", "2529": "ok", "2586": "err", "2587": "ok", "2588": "ok", "2624": "minor", "2648": "err", "2653": "ok", "2696": "ok", "2698": "ok", "2782": "ok", "2856": "err", "2912": "ok", "3047": "ok", "3075": "ok", "3109": "err", "3127": "ok", "3240": "ok", "3304": "minor", "3310": "minor", "3336": "ok", "3337": "ok", "3346": "ok", "3348": "ok", "3358": "minor", "3412": "ok", "3439": "ok"}      # старые 80: err / minor / ok
FRESH = {"3042": "x", "163": "err", "1368": "err", "1393": "err", "2771": "x", "1701": "err", "1550": "x", "463": "x", "2307": "x", "3495": "err", "3330": "err", "1846": "x", "1073": "x", "2000": "x", "3226": "x", "2567": "x", "168": "x", "2196": "err", "2119": "err", "2364": "x", "3252": "x", "2558": "err", "218": "x", "1815": "err", "3251": "x", "991": "x", "37": "x", "2626": "x", "1579": "x", "605": "err", "340": "x", "3218": "x", "39": "x", "2305": "x", "3174": "x", "122": "x", "1959": "x", "4": "x", "3084": "x", "3277": "err", "2326": "x", "1941": "x", "1328": "x", "1854": "x", "1728": "err", "3305": "x", "371": "x", "2601": "x", "1038": "x", "2110": "x", "657": "x", "1258": "x", "1996": "x", "1516": "x", "481": "x", "690": "err", "2107": "x", "566": "x", "761": "x", "720": "x"}      # свежие 60 (30 худших по tev1 + 30 случайных): err / x

def auc(pos, neg):
    s = sum((p > n) + 0.5 * (p == n) for p in pos for n in neg)
    return s / (len(pos) * len(neg))

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("log")
    ap.add_argument("--sample", default="/books/.dm/sample_results.jsonl")
    ap.add_argument("--threads", type=int, default=6)
    ap.add_argument("--out", default="/books/.dm/kiwi_results.jsonl")
    a = ap.parse_args()
    torch.set_num_threads(a.threads)
    lines = open(a.log, encoding="utf-8").read().splitlines()
    sample = [json.loads(l)["line"] for l in open(a.sample, encoding="utf-8")]
    todo = sorted(set(sample) | {int(k) for k in LAB80})
    print(f"Абзацев: {len(todo)} (выборка {len(sample)} + размеченные 80), потоков CPU: {a.threads}", flush=True)
    t0 = time.time()
    model = load_from_checkpoint(download_model("Unbabel/wmt22-cometkiwi-da"))
    print(f"Модель загружена за {time.time()-t0:.0f} с", flush=True)

    items, owners = [], []          # (src, mt) и (строка, номер предложения или -1)
    for ln in todo:
        j = json.loads(lines[ln])
        items.append({"src": j["src"], "mt": j["out"]}); owners.append((ln, -1))
        en, ru = split_en(j["src"]), split_ru(j["out"])
        if len(en) == len(ru) and len(en) > 1:
            for k, (e, r) in enumerate(zip(en, ru)):
                items.append({"src": e, "mt": r}); owners.append((ln, k))
    print(f"Запросов: {len(items)} (абзацы + предложения)", flush=True)
    scores, t1 = [], time.time()
    for i in range(0, len(items), 64):
        scores += model.predict(items[i:i+64], batch_size=8, gpus=0, progress_bar=False).scores
        done = len(scores)
        if done % 256 == 0 or done == len(items):
            print(f"  {done}/{len(items)}, {(time.time()-t1)/done:.2f} с/запрос", flush=True)
    dt = time.time() - t1
    res = {ln: {"line": ln, "par": None, "sents": []} for ln in todo}
    for (ln, k), s in zip(owners, scores):
        if k < 0: res[ln]["par"] = s
        else: res[ln]["sents"].append(s)
    with open(a.out, "w", encoding="utf-8") as f:
        for r in res.values():
            f.write(json.dumps(r) + "\n")
    print(f"\nСкорость: {dt/len(items):.2f} с на запрос, {dt/len(todo):.2f} с на абзац вместе с предложениями")
    bad = lambda r, k: 1 - (r["par"] if k == "par" else min(r["sents"]) if r["sents"] else r["par"])
    for name, labs in (("старые 80", LAB80), ("свежие 60", FRESH)):
        rows = [res[int(l)] | {"lab": v} for l, v in labs.items() if int(l) in res]
        E = [r for r in rows if r["lab"] == "err"]
        print(f"\n{name}: абзацев {len(rows)}, ошибок {len(E)}")
        print(f"{'способ':8} {'AUC err/все':>11} {'err в топ-10':>12} {'err в топ-20':>12}")
        for k in ("par", "sent"):
            e = [bad(r, k) for r in E]; rest = [bad(r, k) for r in rows if r["lab"] != "err"]
            top = sorted(rows, key=lambda r: -bad(r, k))
            print(f"{k:8} {auc(e, rest):11.2f} {sum(r['lab']=='err' for r in top[:10]):12} {sum(r['lab']=='err' for r in top[:20]):12}")
    print(f"\nФайл: books/.dm/kiwi_results.jsonl")

if __name__ == "__main__":
    main()
