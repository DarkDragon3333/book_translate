r"""Проверка повторного перевода: исправит ли повтор известные ошибки Rosetta (28 абзацев Spring in Action).
Варианты: Rosetta t=0; Rosetta t=0.4; Rosetta t=0.2 с подсказкой нужного термина в глоссарии; translategemma-gpu.
Запуск (из C:\book_translate, основная Ollama должна работать):
  docker.exe run --rm --network book_translate_default -v C:\book_translate\experiments\decision:/w -v C:\book_translate\books:/books --entrypoint python book_translate/pdf-epub:1 /w/retry_test.py "/books/Spring.in.Action_epub/.rosetta_log.jsonl"
"""
import json, sys, time, urllib.request
ERR = [163, 431, 605, 690, 854, 1141, 1368, 1393, 1631, 1701, 1728, 1815, 1875, 1938, 2018, 2025, 2119, 2196, 2241, 2252, 2558, 2586, 2648, 2856, 3109, 3277, 3330, 3495]
HINTS = {int(k): v for k, v in {"163": [["parent POM", "родительский POM"]], "431": [["listing", "листинг"]], "605": [["inject", "внедрять"], ["autowiring", "автосвязывание"]], "690": [["identity", "идентификатор"]], "854": [["columnstore database", "колоночная база данных"]], "1141": [["Property injection", "внедрение через свойства"]], "1368": [["catch", "перехватить"]], "1631": [["starter dependency", "стартовая зависимость (стартер)"]], "1728": [["injected", "внедрённый"], ["unconverted", "непреобразованный"]], "1815": [["overhead display", "подвесной дисплей"]], "1875": [["Receiving applications", "принимающие приложения"]], "1938": [["bean method", "метод бина"]], "2018": [["service activator", "активатор сервиса"]], "2025": [["integration flow", "интеграционный поток"]], "2119": [["classpath", "classpath"]], "2241": [["zipped together", "объединены попарно"]], "2558": [["in-memory", "в памяти"]], "2586": [["runtime application context", "контекст приложения во время выполнения"], ["bean", "бин"]], "2648": [["persistence", "хранение данных"]], "3277": [["container image", "образ контейнера"], ["image", "образ"]], "3330": [["pod", "под"], ["readiness", "готовность"]], "3495": [["camelCase", "camelCase"]]}.items()}
URL = "http://ollama:11434/api/chat"
CONTEXT = "A technical book about software development."
TONE = "Clear, natural technical prose, as in a professionally edited book."

def rosetta_system(terms, has_marks):
    s = ["Translate the user's text to Russian.", "Context: " + CONTEXT, "Tone: " + TONE]
    if terms:
        s.append("Glossary:"); s += ["- %s -> %s" % (a, b) for a, b in terms]
    if has_marks:
        s.append("Keep placeholders like ⟦code1⟧ and ⟦var2⟧ exactly as they are, at the right place.")
    s += ["Output format: JSON", "Provide the final translation immediately without any other text."]
    return "\n".join(s)

def chat(model, messages, temperature):
    body = json.dumps({"model": model, "stream": False, "options": {"temperature": temperature},
                       "messages": messages}, ensure_ascii=False).encode()
    req = urllib.request.Request(URL, data=body, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=900) as r:
        return json.load(r)["message"]["content"]

def rosetta(src, terms, t):
    raw = chat("rosetta-ru", [{"role": "system", "content": rosetta_system(terms, "⟦" in src)},
                              {"role": "user", "content": json.dumps({"text": src}, ensure_ascii=False)}], t)
    try:
        return json.loads(raw[raw.index("{"):raw.rindex("}") + 1])["text"]
    except Exception:
        return raw

def gemma(src, terms):
    g = ("<glossary>\n" + "\n".join("%s → %s" % (a, b) for a, b in terms) + "\n</glossary>\n") if terms else ""
    ask = g + "Please help me to translate,`%s` to Russian, please return only translated content not include the origin text" % src
    return chat("translategemma-gpu", [{"role": "user", "content": ask}], 0.2)

def main():
    lines = open(sys.argv[1], encoding="utf-8").read().splitlines()
    rows = {}
    for ln in ERR:
        j = json.loads(lines[ln])
        rows[ln] = {"line": ln, "orig": j["out"], "terms": j.get("terms") or []}
    t0 = time.time()
    for n, (ln, r) in enumerate(rows.items(), 1):            # сначала все запросы к Rosetta, потом к translategemma
        src = json.loads(lines[ln])["src"]
        r["t0"] = rosetta(src, r["terms"], 0.0)
        r["t04"] = rosetta(src, r["terms"], 0.4)
        if ln in HINTS:
            r["hint"] = rosetta(src, [t for t in r["terms"] if t[0] not in {h[0] for h in HINTS[ln]}] + HINTS[ln], 0.2)
        if n % 7 == 0: print(f"  Rosetta {n}/{len(rows)}, {time.time()-t0:.0f} с", flush=True)
    for n, (ln, r) in enumerate(rows.items(), 1):
        r["gemma"] = gemma(json.loads(lines[ln])["src"], r["terms"])
        if n % 7 == 0: print(f"  translategemma {n}/{len(rows)}, {time.time()-t0:.0f} с", flush=True)
    with open("/books/.dm/retry_results.jsonl", "w", encoding="utf-8") as f:
        for r in rows.values():
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    print(f"Готово за {(time.time()-t0)/60:.1f} мин. Файл: books/.dm/retry_results.jsonl")

if __name__ == "__main__":
    main()
