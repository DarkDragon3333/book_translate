r"""Проверка по предложениям: tev1 оценивает каждую пару предложений EN/RU, оценка абзаца — худшая.
Те же 80 абзацев Spring in Action с разметкой Claude (номера строк журнала).
Запуск (из C:\book_translate):
  docker.exe run --rm --network bt_decision_default -v C:\book_translate\experiments\decision:/w -v C:\book_translate\books:/books --entrypoint python book_translate/pdf-epub:1 /w/dm_sentences.py "/books/Spring.in.Action_epub/.rosetta_log.jsonl"
"""
import argparse, json, time, urllib.request
import re
ABBR_EN = r"(?<!\be\.g)(?<!\bi\.e)(?<!\betc)(?<!\bvs)(?<!\bMr)(?<!\bDr)(?<!\bNo)(?<!\bFig)"
EN = re.compile(ABBR_EN + r"(?<=[.!?])[\"”’)]?\s+(?=[A-Z⟦\"“(])")
RU = re.compile(r"(?<!\bт\.е)(?<!\bт\.д)(?<!\bт\.п)(?<!\bрис)(?<!\bсм)(?<=[.!?…])[»\")]?\s+(?=[А-ЯЁA-Z⟦«\"(])")
def split_en(t): return [s for s in EN.split(t.strip()) if s.strip()]
def split_ru(t): return [s for s in RU.split(t.strip()) if s.strip()]

LABELS = {"64": "ok", "102": "ok", "148": "ok", "153": "ok", "185": "ok", "191": "minor", "198": "minor", "267": "minor", "315": "ok", "418": "minor", "456": "ok", "457": "minor", "536": "ok", "610": "minor", "651": "ok", "667": "ok", "674": "ok", "706": "ok", "743": "ok", "833": "ok", "934": "minor", "1015": "ok", "1069": "ok", "1083": "minor", "1141": "err", "1197": "ok", "1267": "ok", "1335": "minor", "1347": "ok", "1394": "ok", "1411": "minor", "1468": "ok", "1631": "err", "1646": "ok", "1724": "ok", "1817": "ok", "1875": "err", "1969": "ok", "1971": "minor", "1992": "minor", "2020": "ok", "2025": "err", "2090": "ok", "2116": "ok", "2138": "ok", "2188": "minor", "2241": "err", "2252": "err", "2268": "ok", "2314": "minor", "2376": "ok", "2454": "ok", "2464": "minor", "2480": "ok", "2529": "ok", "2586": "err", "2587": "ok", "2588": "ok", "2624": "minor", "2648": "err", "2653": "ok", "2696": "ok", "2698": "ok", "2782": "ok", "2856": "err", "2912": "ok", "3047": "ok", "3075": "ok", "3109": "err", "3127": "ok", "3240": "ok", "3304": "minor", "3310": "minor", "3336": "ok", "3337": "ok", "3346": "ok", "3348": "ok", "3358": "minor", "3412": "ok", "3439": "ok"}
INSTR = ("The state has an English source text (EN) and its Russian translation (RU). "
         "Does the Russian translation preserve the meaning of the English source?")

def ask(url, model, en, ru):
    body = {"model": model, "state": f"EN: {en}\nRU: {ru}", "keep_alive": "10m",
            "questions": {"ok": {"type": "noul", "instructions": INSTR}}}
    req = urllib.request.Request(url + "/v1/systemone", data=json.dumps(body, ensure_ascii=False).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=600) as r:
        return json.load(r)["answers"]["ok"]["noul"]

def auc(pos, neg):
    s = sum((p > n) + 0.5 * (p == n) for p in pos for n in neg)
    return s / (len(pos) * len(neg))

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("log")
    ap.add_argument("--url", default="http://ollama-dm:11434")
    ap.add_argument("--model", default="tev1")
    ap.add_argument("--out", default="/books/.dm/sentences_results.jsonl")
    a = ap.parse_args()
    lines = open(a.log, encoding="utf-8").read().splitlines()
    tot = al = 0
    for line in lines:
        j = json.loads(line)
        if isinstance(j.get("src"), str) and isinstance(j.get("out"), str) and len(j["src"]) >= 80:
            tot += 1
            al += len(split_en(j["src"])) == len(split_ru(j["out"]))
    print(f"Во всём журнале число предложений совпадает в {al} из {tot} абзацев ({al/tot:.1%})", flush=True)

    rows, t0, nreq = [], time.time(), 0
    for n, (ln, lab) in enumerate(sorted(LABELS.items(), key=lambda kv: int(kv[0])), 1):
        j = json.loads(lines[int(ln)])
        en, ru = split_en(j["src"]), split_ru(j["out"])
        bad_par = 1 - ask(a.url, a.model, j["src"], j["out"]); nreq += 1
        if len(en) == len(ru) and len(en) > 1:
            sb = [1 - ask(a.url, a.model, e, r) for e, r in zip(en, ru)]; nreq += len(sb)
            bad_sent = max(sb)
        elif len(en) != len(ru):
            bad_sent = 1.0           # число предложений разошлось — сразу подозрение
        else:
            bad_sent = bad_par       # одно предложение — то же, что абзац
        rows.append({"line": int(ln), "lab": lab, "n": len(en), "par": bad_par, "sent": bad_sent,
                     "both": max(bad_par, bad_sent)})
        if n % 20 == 0:
            print(f"  {n}/{len(LABELS)}, {(time.time()-t0)/n:.1f} с/абзац", flush=True)
    with open(a.out, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")
    E = [r for r in rows if r["lab"] == "err"]
    print(f"\nАбзацев {len(rows)}, запросов {nreq}; err {len(E)} (из них многопредложенческих {sum(r['n']>1 for r in E)})")
    print(f"{'способ':8} {'AUC err/ok':>10} {'AUC err/все':>11} {'err в топ-10':>12} {'err в топ-20':>12}")
    for k in ("par", "sent", "both"):
        e = [r[k] for r in E]
        o = [r[k] for r in rows if r["lab"] == "ok"]
        rest = [r[k] for r in rows if r["lab"] != "err"]
        top = sorted(rows, key=lambda r: -r[k])
        print(f"{k:8} {auc(e, o):10.2f} {auc(e, rest):11.2f} {sum(r['lab']=='err' for r in top[:10]):12} "
              f"{sum(r['lab']=='err' for r in top[:20]):12}")

if __name__ == "__main__":
    main()
