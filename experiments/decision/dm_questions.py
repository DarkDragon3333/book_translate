r"""Сравнение вопросов к tev1 на 80 абзацах Spring in Action, размеченных Claude.
Разметка — только номера строк журнала: err — явное искажение смысла, minor — мелочь, ok — без ошибок.
Запуск (из C:\book_translate):
  docker.exe run --rm --network bt_decision_default -v C:\book_translate\experiments\decision:/w -v C:\book_translate\books:/books --entrypoint python book_translate/pdf-epub:1 /w/dm_questions.py "/books/Spring.in.Action_epub/.rosetta_log.jsonl"
"""
import argparse, json, time, urllib.request

LABELS = {"64": "ok", "102": "ok", "148": "ok", "153": "ok", "185": "ok", "191": "minor", "198": "minor", "267": "minor", "315": "ok", "418": "minor", "456": "ok", "457": "minor", "536": "ok", "610": "minor", "651": "ok", "667": "ok", "674": "ok", "706": "ok", "743": "ok", "833": "ok", "934": "minor", "1015": "ok", "1069": "ok", "1083": "minor", "1141": "err", "1197": "ok", "1267": "ok", "1335": "minor", "1347": "ok", "1394": "ok", "1411": "minor", "1468": "ok", "1631": "err", "1646": "ok", "1724": "ok", "1817": "ok", "1875": "err", "1969": "ok", "1971": "minor", "1992": "minor", "2020": "ok", "2025": "err", "2090": "ok", "2116": "ok", "2138": "ok", "2188": "minor", "2241": "err", "2252": "err", "2268": "ok", "2314": "minor", "2376": "ok", "2454": "ok", "2464": "minor", "2480": "ok", "2529": "ok", "2586": "err", "2587": "ok", "2588": "ok", "2624": "minor", "2648": "err", "2653": "ok", "2696": "ok", "2698": "ok", "2782": "ok", "2856": "err", "2912": "ok", "3047": "ok", "3075": "ok", "3109": "err", "3127": "ok", "3240": "ok", "3304": "minor", "3310": "minor", "3336": "ok", "3337": "ok", "3346": "ok", "3348": "ok", "3358": "minor", "3412": "ok", "3439": "ok"}

# (имя, тип, инструкция, критерии, как получить «плохость» 0..1)
Q = {
    "ok":      ("noul", "The state has an English source text (EN) and its Russian translation (RU). "
                        "Does the Russian translation preserve the meaning of the English source?", None, "1-p"),
    "missing": ("noul", "The state has an English source text (EN) and its Russian translation (RU). "
                        "Is any information from the English source missing or lost in the Russian translation?", None, "p"),
    "added":   ("noul", "The state has an English source text (EN) and its Russian translation (RU). "
                        "Does the Russian translation add or invent information that is not in the English source?", None, "p"),
    "wrong":   ("noul", "The state has an English source text (EN) and its Russian translation (RU). "
                        "Does the Russian translation contain a mistranslated word or phrase that changes what the text says?", None, "p"),
    "terms":   ("noul", "The state has an English source text (EN) about software development and its Russian translation (RU). "
                        "Is every technical term in RU either the standard Russian term for it or kept in English?", None, "1-p"),
    "grade":   ("score", "The state has an English source text (EN) and its Russian translation (RU). "
                         "How accurate is the Russian translation?",
                ["Wrong meaning", "Partly wrong", "Accurate but awkward", "Accurate"], "score"),
}

def ask(url, model, en, ru):
    qs = {}
    for k, (t, ins, crit, _) in Q.items():
        qs[k] = {"type": t, "instructions": ins}
        if crit:
            qs[k]["criteria"] = crit
    body = {"model": model, "state": f"EN: {en}\nRU: {ru}", "keep_alive": "10m", "questions": qs}
    req = urllib.request.Request(url + "/v1/systemone", data=json.dumps(body, ensure_ascii=False).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=600) as r:
        ans = json.load(r)["answers"]
    out = {}
    for k, (t, _, crit, how) in Q.items():
        a = ans[k]
        if t == "noul":
            out[k] = a["noul"] if how == "p" else 1 - a["noul"]
        else:  # score: вероятность двух худших оценок
            pr = a.get("probabilities") or {}
            out[k] = float(pr.get("0", 0)) + float(pr.get("1", 0))
    return out

def auc(pos, neg):
    s = sum((p > n) + 0.5 * (p == n) for p in pos for n in neg)
    return s / (len(pos) * len(neg))

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("log")
    ap.add_argument("--url", default="http://ollama-dm:11434")
    ap.add_argument("--model", default="tev1")
    ap.add_argument("--out", default="/books/.dm/questions_results.jsonl")
    a = ap.parse_args()
    lines = open(a.log, encoding="utf-8").read().splitlines()
    rows, t0 = [], time.time()
    for n, (ln, lab) in enumerate(sorted(LABELS.items(), key=lambda kv: int(kv[0])), 1):
        j = json.loads(lines[int(ln)])
        bad = ask(a.url, a.model, j["src"], j["out"])
        rows.append({"line": int(ln), "lab": lab, **bad})
        if n % 20 == 0:
            print(f"  {n}/{len(LABELS)}, {(time.time()-t0)/n:.1f} с/абзац", flush=True)
    with open(a.out, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")
    keys = list(Q) + ["mean", "max"]
    for r in rows:
        v = [r[k] for k in Q if k != "grade"]
        r["mean"], r["max"] = sum(v) / len(v), max(v)
    E = [r for r in rows if r["lab"] == "err"]
    print(f"\nАбзацев {len(rows)}: err {len(E)}, minor {sum(r['lab']=='minor' for r in rows)}, ok {sum(r['lab']=='ok' for r in rows)}")
    print(f"{'вопрос':8} {'AUC err/ok':>10} {'AUC err/все':>11} {'err в топ-10':>12} {'err в топ-20':>12}")
    for k in keys:
        e = [r[k] for r in E]
        o = [r[k] for r in rows if r["lab"] == "ok"]
        rest = [r[k] for r in rows if r["lab"] != "err"]
        top = sorted(rows, key=lambda r: -r[k])
        t10 = sum(r["lab"] == "err" for r in top[:10])
        t20 = sum(r["lab"] == "err" for r in top[:20])
        print(f"{k:8} {auc(e, o):10.2f} {auc(e, rest):11.2f} {t10:12} {t20:12}")

if __name__ == "__main__":
    main()
