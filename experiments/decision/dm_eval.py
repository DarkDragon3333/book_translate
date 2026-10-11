"""Оценка моделей принятия решений (/v1/systemone) как проверки смысла перевода.
Запуск (из C:\\book_translate):
  docker.exe run --rm --network bt_decision_default -v ${PWD}\\experiments\\decision:/w --entrypoint python book_translate/pdf-epub:1 /w/dm_eval.py
"""
import argparse, json, sys, time, urllib.request
from collections import defaultdict
sys.path.insert(0, "/w")
from synth import ITEMS

INSTR = {
    "A": "The state has an English source sentence (EN) and its Russian translation (RU). "
         "Does the Russian translation preserve the meaning of the English source?",
    "B": "Compare the English source (EN) with the Russian translation (RU). Is RU an accurate "
         "translation with the same meaning: nothing added, nothing missing, no negation flipped, "
         "no numbers changed, no roles swapped?",
}

def ask(url, model, state, instr):
    body = {"model": model, "state": state, "keep_alive": "10m",
            "questions": {"ok": {"type": "noul", "instructions": instr}}}
    req = urllib.request.Request(url + "/v1/systemone", data=json.dumps(body, ensure_ascii=False).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=600) as r:
        return json.load(r)["answers"]["ok"]["noul"]

def auc(pos, neg):  # вероятность, что верный перевод получил больше, чем искажённый
    s = sum((p > n) + 0.5 * (p == n) for p in pos for n in neg)
    return s / (len(pos) * len(neg))

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://ollama-dm:11434")
    ap.add_argument("--models", default="tev1:0.8b,tev1")
    ap.add_argument("--out", default="/w/dm_eval_results.jsonl")
    a = ap.parse_args()
    rows = []
    for model in a.models.split(","):
        ask(a.url, model, "EN: test\nRU: тест", INSTR["A"])  # загрузка модели
        for ik, instr in INSTR.items():
            t0 = time.time()
            for i, (en, good, para, bad, kind) in enumerate(ITEMS):
                for var, ru in (("good", good), ("para", para), ("bad", bad)):
                    p = ask(a.url, model, f"EN: {en}\nRU: {ru}", instr)
                    rows.append({"model": model, "instr": ik, "id": i, "var": var, "kind": kind, "p": p})
            n = 3 * len(ITEMS)
            print(f"{model} {ik}: {n} запросов, {(time.time()-t0)/n*1000:.0f} мс/запрос", flush=True)
    with open(a.out, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")

    print()
    print(f"{'модель':10} {'инстр':5} {'AUC g/b':>7} {'AUC p/b':>7} {'ср.good':>7} {'ср.para':>7} {'ср.bad':>6} "
          f"{'порог':>5} {'пойман':>6} {'ложн.g':>6} {'ложн.p':>6}")
    best = {}
    for model in a.models.split(","):
        for ik in INSTR:
            sel = [r for r in rows if r["model"] == model and r["instr"] == ik]
            g = [r["p"] for r in sel if r["var"] == "good"]
            pa = [r["p"] for r in sel if r["var"] == "para"]
            b = [r["p"] for r in sel if r["var"] == "bad"]
            # порог: ложных тревог на good+para не больше 10%, ловим как можно больше
            ok = sorted(g + pa)
            thr = ok[int(len(ok) * 0.10)]
            caught = sum(x < thr for x in b) / len(b)
            fg = sum(x < thr for x in g) / len(g)
            fp = sum(x < thr for x in pa) / len(pa)
            print(f"{model:10} {ik:5} {auc(g,b):7.2f} {auc(pa,b):7.2f} {sum(g)/len(g):7.2f} {sum(pa)/len(pa):7.2f} "
                  f"{sum(b)/len(b):6.2f} {thr:5.2f} {caught:6.0%} {fg:6.0%} {fp:6.0%}")
            best[(model, ik)] = (auc(pa, b), thr, sel)
    (model, ik), (_, thr, sel) = max(best.items(), key=lambda kv: kv[1][0])
    print(f"\nПо типам искажений (лучшая связка {model} {ik}, порог {thr:.2f}): пойман / всего")
    by = defaultdict(lambda: [0, 0])
    for r in sel:
        if r["var"] == "bad":
            by[r["kind"]][1] += 1
            by[r["kind"]][0] += r["p"] < thr
    print("  " + ", ".join(f"{k} {c}/{n}" for k, (c, n) in sorted(by.items(), key=lambda kv: -kv[1][1])))

if __name__ == "__main__":
    main()
