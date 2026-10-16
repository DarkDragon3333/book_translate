r"""xCOMET-lite без эталона на журналах адаптера: перевод Rosetta-4B и (для проверки) Rosetta-12B тех же абзацев.

Набор абзацев задаёт журнал 12B (одна глава); абзацы 4B ищутся в его журнале по тексту оригинала
(без меток и пробелов — журнал 4B может быть от полной книги). Метки ⟦…⟧ остаются как есть — они одинаковы в оригинале и переводе.

  python /w/qe_score.py --set spring --log4 /books/Spring_test/.rosetta_log.jsonl \
      --log12 /books/Spring_rosetta12/.rosetta_log.jsonl --out /books/.qe/xcl_spring.jsonl

Пишет по строке на абзац: set, i (номер строки в журнале 12B), line4, s4/s12 (оценка 0..1, выше — лучше),
spans4/spans12 (места ошибок: text, severity minor/major/critical, confidence, start, end). Текст книги в выход не пишется
(кроме коротких отрезков ошибок) — для сверки с разметкой Claude.
"""
import argparse, json, os, re, sys, time
import torch

KEY = re.compile(r"⟦[^⟧]*⟧|[^\w]", re.U)
def key(s): return KEY.sub("", s).lower()

def load(p):
    out = []
    with open(p, encoding="utf-8") as f:
        for n, l in enumerate(f):
            l = l.strip()
            if l:
                r = json.loads(l); r["_line"] = n; out.append(r)
    return out

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--set", required=True)
    ap.add_argument("--log4", required=True)
    ap.add_argument("--log12", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--threads", type=int, default=8)
    ap.add_argument("--batch", type=int, default=8)
    ap.add_argument("--model", default="myyycroft/XCOMET-lite")
    a = ap.parse_args()
    torch.set_num_threads(a.threads)

    r12, r4 = load(a.log12), load(a.log4)
    idx4 = {}
    for r in r4:
        idx4.setdefault(key(r["src"]), r)          # первый перевод абзаца (повторы одинакового текста — заголовки и т. п.)
    pairs, miss = [], 0
    for i, r in enumerate(r12):
        if len(key(r["src"])) < 3:
            continue
        q = idx4.get(key(r["src"]))
        if q is None:
            miss += 1; continue
        pairs.append((i, r, q))
    print(f"[{a.set}] абзацев в журнале 12B: {len(r12)}, найдено в журнале 4B: {len(pairs)}, не найдено: {miss}", flush=True)

    from deberta_encoder import XCOMETLite
    t0 = time.time()
    model = XCOMETLite.from_pretrained(a.model)
    model.eval()
    print(f"Модель загружена за {time.time()-t0:.0f} с, потоков CPU: {a.threads}", flush=True)

    def run(field):
        # у каждого перевода свой оригинал: метки ⟦var⟧ у журналов 4B и 12B могут различаться (правка курсива 12.10)
        data = [{"src": (q if field == "4" else r)["src"], "mt": (q if field == "4" else r)["out"]} for i, r, q in pairs]
        t = time.time(); scores, spans = [], []
        for k in range(0, len(data), 64):
            o = model.predict(data[k:k+64], batch_size=a.batch, gpus=0, progress_bar=False)
            scores += [float(s) for s in o.scores]
            spans += o.metadata.error_spans
            print(f"  {field}B: {len(scores)}/{len(data)}, {(time.time()-t)/len(scores):.2f} с/абзац", flush=True)
        return scores, spans, time.time() - t

    s4, sp4, t4 = run("4")
    s12, sp12, t12 = run("12")
    os.makedirs(os.path.dirname(a.out) or ".", exist_ok=True)
    clean = lambda sp: [{"text": x["text"], "severity": x["severity"], "confidence": round(float(x["confidence"]), 3),
                         "start": x["start"], "end": x["end"]} for x in sp]
    with open(a.out, "w", encoding="utf-8") as f:
        for (i, r, q), a4, b4, a12, b12 in zip(pairs, s4, sp4, s12, sp12):
            f.write(json.dumps({"set": a.set, "i": i, "line4": q["_line"], "len": len(r["src"]),
                                "s4": round(a4, 4), "spans4": clean(b4), "s12": round(a12, 4), "spans12": clean(b12)},
                               ensure_ascii=False) + "\n")
    n = len(pairs)
    print(f"\nГотово: {a.out}")
    print(f"Скорость: 4B {t4/n:.2f} с/абзац, 12B {t12/n:.2f} с/абзац")
    print(f"Средняя оценка: 4B {sum(s4)/n:.3f}, 12B {sum(s12)/n:.3f} (ожидаем 12B выше)")
    print(f"Абзацев с местами ошибок: 4B {sum(bool(x) for x in sp4)}, 12B {sum(bool(x) for x in sp12)}")

if __name__ == "__main__":
    main()
