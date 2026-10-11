r"""Свежая выборка для проверки по предложениям: случайные абзацы журнала, кроме 300 из прошлых прогонов.
Идёт в случайном порядке до лимита времени, поэтому любой готовый кусок — честная случайная выборка.
Запуск (из C:\book_translate):
  docker.exe run --rm --network bt_decision_default -v C:\book_translate\experiments\decision:/w -v C:\book_translate\books:/books --entrypoint python book_translate/pdf-epub:1 /w/dm_sample.py "/books/Spring.in.Action_epub/.rosetta_log.jsonl" --minutes 20
"""
import argparse, json, random, re, time, urllib.request
import re
ABBR_EN = r"(?<!\be\.g)(?<!\bi\.e)(?<!\betc)(?<!\bvs)(?<!\bMr)(?<!\bDr)(?<!\bNo)(?<!\bFig)"
EN = re.compile(ABBR_EN + r"(?<=[.!?])[\"”’)]?\s+(?=[A-Z⟦\"“(])")
RU = re.compile(r"(?<!\bт\.е)(?<!\bт\.д)(?<!\bт\.п)(?<!\bрис)(?<!\bсм)(?<=[.!?…])[»\")]?\s+(?=[А-ЯЁA-Z⟦«\"(])")
def split_en(t): return [s for s in EN.split(t.strip()) if s.strip()]
def split_ru(t): return [s for s in RU.split(t.strip()) if s.strip()]

USED = set([15, 29, 48, 60, 64, 84, 102, 111, 113, 116, 117, 138, 143, 148, 153, 166, 183, 185, 188, 191, 198, 213, 225, 229, 251, 253, 261, 267, 269, 289, 305, 315, 370, 389, 418, 429, 450, 456, 457, 472, 494, 529, 536, 550, 564, 592, 610, 630, 645, 649, 651, 667, 674, 683, 705, 706, 709, 714, 739, 740, 741, 743, 745, 747, 764, 786, 804, 807, 828, 833, 848, 855, 859, 860, 934, 945, 1004, 1015, 1047, 1050, 1054, 1061, 1069, 1083, 1086, 1089, 1092, 1097, 1100, 1105, 1112, 1141, 1161, 1182, 1187, 1197, 1199, 1208, 1233, 1261, 1262, 1265, 1267, 1312, 1317, 1332, 1335, 1347, 1351, 1369, 1375, 1380, 1388, 1394, 1401, 1403, 1411, 1419, 1432, 1438, 1445, 1452, 1456, 1466, 1468, 1498, 1506, 1512, 1548, 1562, 1567, 1568, 1575, 1576, 1590, 1631, 1632, 1634, 1646, 1656, 1660, 1680, 1705, 1724, 1725, 1741, 1777, 1781, 1784, 1809, 1811, 1816, 1817, 1818, 1820, 1829, 1832, 1859, 1875, 1891, 1913, 1920, 1940, 1954, 1969, 1971, 1975, 1992, 2003, 2020, 2025, 2027, 2090, 2091, 2097, 2102, 2113, 2116, 2118, 2124, 2129, 2138, 2167, 2188, 2240, 2241, 2252, 2258, 2268, 2290, 2313, 2314, 2318, 2320, 2324, 2328, 2347, 2360, 2375, 2376, 2394, 2395, 2396, 2406, 2416, 2454, 2463, 2464, 2480, 2514, 2519, 2529, 2559, 2565, 2586, 2587, 2588, 2607, 2619, 2624, 2625, 2635, 2648, 2653, 2696, 2698, 2725, 2731, 2746, 2757, 2772, 2774, 2780, 2782, 2789, 2856, 2905, 2912, 2929, 2967, 2971, 2989, 2991, 3006, 3011, 3015, 3028, 3046, 3047, 3051, 3067, 3070, 3075, 3082, 3091, 3096, 3098, 3101, 3102, 3109, 3118, 3121, 3127, 3140, 3152, 3165, 3177, 3188, 3210, 3239, 3240, 3263, 3273, 3290, 3291, 3296, 3298, 3304, 3310, 3319, 3335, 3336, 3337, 3339, 3346, 3348, 3351, 3358, 3367, 3381, 3407, 3412, 3424, 3430, 3439, 3451, 3498, 3501, 3504, 3512])
MARK = re.compile(r"⟦[^⟧]*⟧")
INSTR = ("The state has an English source text (EN) and its Russian translation (RU). "
         "Does the Russian translation preserve the meaning of the English source?")

def ask(url, model, en, ru):
    body = {"model": model, "state": f"EN: {en}\nRU: {ru}", "keep_alive": "10m",
            "questions": {"ok": {"type": "noul", "instructions": INSTR}}}
    req = urllib.request.Request(url + "/v1/systemone", data=json.dumps(body, ensure_ascii=False).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=600) as r:
        return json.load(r)["answers"]["ok"]["noul"]

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("log")
    ap.add_argument("--url", default="http://ollama-dm:11434")
    ap.add_argument("--model", default="tev1")
    ap.add_argument("--minutes", type=float, default=20)
    ap.add_argument("--seed", type=int, default=2)
    ap.add_argument("--out", default="/books/.dm/sample_results.jsonl")
    a = ap.parse_args()
    lines = open(a.log, encoding="utf-8").read().splitlines()
    pool = []
    for i, line in enumerate(lines):
        if i in USED:
            continue
        j = json.loads(line)
        en, ru = j.get("src"), j.get("out")
        if isinstance(en, str) and isinstance(ru, str) and len(MARK.sub("", en)) >= 80                 and len(en) + len(ru) <= 2600 and re.search("[А-Яа-яЁё]", ru):
            pool.append(i)
    random.Random(a.seed).shuffle(pool)
    print(f"Подходящих абзацев {len(pool)}, лимит {a.minutes:g} мин", flush=True)
    ask(a.url, a.model, "test", "тест")
    t0, done, mism = time.time(), 0, 0
    with open(a.out, "w", encoding="utf-8") as f:
        for i in pool:
            if time.time() - t0 > a.minutes * 60:
                break
            j = json.loads(lines[i])
            en, ru = split_en(j["src"]), split_ru(j["out"])
            par = ask(a.url, a.model, j["src"], j["out"])
            sents = [ask(a.url, a.model, e, r) for e, r in zip(en, ru)] if len(en) == len(ru) and len(en) > 1 else []
            mism += len(en) != len(ru)
            f.write(json.dumps({"line": i, "n_en": len(en), "n_ru": len(ru), "par": par, "sents": sents}) + "\n")
            f.flush()
            done += 1
            if done % 50 == 0:
                print(f"  {done} абзацев, {(time.time()-t0)/done:.1f} с/абзац", flush=True)
    print(f"Готово: {done} абзацев за {(time.time()-t0)/60:.1f} мин; число предложений разошлось в {mism}")
    print("Файл: books/.dm/sample_results.jsonl")

if __name__ == "__main__":
    main()
