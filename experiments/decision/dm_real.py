"""Проверка модели принятия решений на настоящем журнале адаптера Rosetta.
Берёт выборку абзацев (src → out), оценивает настоящие переводы и их испорченные копии:
  swap   — перевод другого абзаца похожей длины;
  cut    — у перевода отрезано последнее предложение;
  number — изменено одно число вне меток.
Итог: ложные тревоги на настоящих переводах и доля пойманных порч при разных порогах,
плюс /books/.dm/review.html — абзацы с самой низкой оценкой для просмотра глазами.
"""
import argparse, html, json, random, re, time, urllib.request
from collections import defaultdict

INSTR = ("The state has an English source text (EN) and its Russian translation (RU). "
         "Does the Russian translation preserve the meaning of the English source?")

def ask(url, model, en, ru):
    body = {"model": model, "state": f"EN: {en}\nRU: {ru}", "keep_alive": "10m",
            "questions": {"ok": {"type": "noul", "instructions": INSTR}}}
    req = urllib.request.Request(url + "/v1/systemone", data=json.dumps(body, ensure_ascii=False).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=600) as r:
        return json.load(r)["answers"]["ok"]["noul"]

CYR = re.compile(r"[А-Яа-яЁё]")
MARK = re.compile(r"⟦[^⟧]*⟧")
SENT = re.compile(r"(?<=[.!?…])\s+(?=[А-ЯЁA-Z⟦«\"(])")

def cut_last(ru):
    parts = SENT.split(ru.strip())
    if len(parts) < 2:
        return None
    short = " ".join(parts[:-1])
    return short if len(short) >= 0.4 * len(ru) else None

def change_number(ru, rnd):
    spans, pos = [], 0
    for m in MARK.finditer(ru):
        spans.append((pos, m.start())); pos = m.end()
    spans.append((pos, len(ru)))
    nums = [(a + m.start(), a + m.end(), m.group()) for a, b in spans
            for m in re.finditer(r"(?<![\d.,])\d{1,6}(?![\d.,]*\d)", ru[a:b])]
    if not nums:
        return None
    s, e, n = rnd.choice(nums)
    v = int(n)
    nv = v + 2 if v < 10 else v * 2
    return ru[:s] + str(nv) + ru[e:]

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("log")
    ap.add_argument("--url", default="http://ollama-dm:11434")
    ap.add_argument("--model", default="tev1")
    ap.add_argument("-n", type=int, default=300)
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--out", default="/books/.dm")
    a = ap.parse_args()
    import os; os.makedirs(a.out, exist_ok=True)
    rnd = random.Random(a.seed)

    pool, skipped = [], defaultdict(int)
    with open(a.log, encoding="utf-8") as f:
        for i, line in enumerate(f):
            try:
                r = json.loads(line)
            except Exception:
                skipped["не JSON"] += 1; continue
            en, ru = r.get("src"), r.get("out")
            if not isinstance(en, str) or not isinstance(ru, str):
                skipped["нет src/out"] += 1; continue
            if len(MARK.sub("", en)) < 80:
                skipped["короткий"] += 1; continue
            if len(en) + len(ru) > 2600:
                skipped["длинный (>~2000 токенов)"] += 1; continue
            if not CYR.search(ru):
                skipped["перевод без кириллицы"] += 1; continue
            pool.append((i, en, ru))
    print(f"Журнал: подходящих абзацев {len(pool)}; пропущено: " +
          ", ".join(f"{k} {v}" for k, v in skipped.items()), flush=True)
    sample = rnd.sample(pool, min(a.n, len(pool)))

    tasks = []
    for i, en, ru in sample:
        tasks.append(("real", i, en, ru))
        others = [o for o in sample if o[0] != i and 0.7 < len(o[2]) / len(ru) < 1.4]
        if others:
            tasks.append(("swap", i, en, rnd.choice(others)[2]))
        c = cut_last(ru)
        if c:
            tasks.append(("cut", i, en, c))
        nn = change_number(ru, rnd)
        if nn:
            tasks.append(("number", i, en, nn))
    print("Запросов: " + ", ".join(f"{k} {sum(t[0]==k for t in tasks)}" for k in ("real", "swap", "cut", "number")), flush=True)

    ask(a.url, a.model, "test", "тест")
    t0, res = time.time(), []
    for k, (kind, i, en, ru) in enumerate(tasks, 1):
        res.append({"kind": kind, "line": i, "p": ask(a.url, a.model, en, ru), "en": en, "ru": ru})
        if k % 100 == 0:
            print(f"  {k}/{len(tasks)}, {(time.time()-t0)/k:.2f} с/запрос", flush=True)
    with open(f"{a.out}/real_results.jsonl", "w", encoding="utf-8") as f:
        for r in res:
            f.write(json.dumps({x: r[x] for x in ("kind", "line", "p")}) + "\n")

    by = defaultdict(list)
    for r in res:
        by[r["kind"]].append(r["p"])
    print(f"\nСредняя оценка: " + ", ".join(f"{k} {sum(v)/len(v):.2f}" for k, v in by.items()))
    print(f"{'порог':>6} {'тревог на настоящих':>20} {'swap':>6} {'cut':>6} {'number':>7}")
    for thr in (0.9, 0.7, 0.5, 0.3, 0.1):
        row = [sum(p < thr for p in by[k]) / max(1, len(by[k])) for k in ("real", "swap", "cut", "number")]
        print(f"{thr:6.1f} {row[0]:20.0%} {row[1]:6.0%} {row[2]:6.0%} {row[3]:7.0%}")

    worst = sorted((r for r in res if r["kind"] == "real"), key=lambda r: r["p"])[:40]
    rows = "".join(
        f"<tr><td>{n}</td><td>{r['p']:.3f}</td><td>{html.escape(r['en'])}</td><td>{html.escape(r['ru'])}</td>"
        f"<td><label><input type=checkbox onchange=upd()> да</label></td></tr>" for n, r in enumerate(worst, 1))
    page = f"""<!doctype html><meta charset=utf-8><title>Проверка смысла: худшие 40</title>
<style>body{{font:15px/1.45 system-ui;margin:16px}}table{{border-collapse:collapse}}td,th{{border:1px solid #ccc;padding:6px;vertical-align:top}}
td:nth-child(3),td:nth-child(4){{width:44%}}#c{{position:sticky;top:0;background:#ffe;padding:8px;font-weight:bold}}</style>
<div id=c></div><p>Настоящие переводы Rosetta с самой низкой оценкой модели. Отметь, где перевод действительно искажает смысл.</p>
<table><tr><th>#</th><th>оценка</th><th>EN</th><th>RU</th><th>Ошибка?</th></tr>{rows}</table>
<script>function upd(){{var b=document.querySelectorAll('input:checked');var n=[];b.forEach(x=>n.push(x.closest('tr').cells[0].textContent));
document.getElementById('c').textContent='Отмечено ошибок: '+b.length+' из {len(worst)}'+(n.length?' (№ '+n.join(', ')+')':'');}}upd();</script>"""
    with open(f"{a.out}/review.html", "w", encoding="utf-8") as f:
        f.write(page)
    print(f"\nПросмотр: books\\.dm\\review.html ({len(worst)} абзацев с самой низкой оценкой)")

if __name__ == "__main__":
    main()
