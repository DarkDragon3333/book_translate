r"""ReMedy-9B через llama-server (эмбеддинг последнего токена) × голова score.npy — те же абзацы, что на Kaggle.

Запрос и токены — как в kaggle_remedy.py (chat-шаблон Gemma + tokenizer(text) из tokenizer.json ReMedy-9B);
серверу передаются готовые номера токенов, так что токенизация совпадает с Kaggle точно.
Пишет /books/.qe/qe_ReMedy-9B-<tag>_<глава>.jsonl в том же формате, что Kaggle.
"""
import argparse, json, os, re, sys, time, urllib.request
import numpy as np

PROMPT = ("Translate the following {s} text into natural, fluent {t} sentence while preserving the original meaning:\n"
          "{s}:{src}\n{t}:")
KEY = re.compile(r"⟦[^⟧]*⟧|[^\w]", re.U)
key = lambda s: KEY.sub("", s).lower()
LOGS = {
    "spring": ("/books/Spring_test/.rosetta_log.jsonl", "/books/Spring_rosetta12/.rosetta_log.jsonl"),
    "geron": ("/books/.archive/Geron 2026 - 35pct 12.10/.rosetta_log.jsonl", "/books/geron_rosetta12/.rosetta_log.jsonl"),
    "geron10": ("/books/geron_ch10_4b/.rosetta_log.jsonl", "/books/geron_ch10_12b/.rosetta_log.jsonl"),  # свежая глава 13.10
    "spring11": ("/books/Spring_c11_4b/.rosetta_log.jsonl", "/books/Spring_c11_12b/.rosetta_log.jsonl"),  # свежая глава 14.10 (Spring гл. 4)
}


def load(p):
    return [json.loads(l) for l in open(p, encoding="utf-8") if l.strip()]


def pairs(tag):
    p4, p12 = LOGS[tag]
    r12, r4 = load(p12), load(p4)
    idx = {}
    for r in r4:
        idx.setdefault(key(r["src"]), r)
    return [(i, r, idx[key(r["src"])]) for i, r in enumerate(r12) if len(key(r["src"])) >= 3 and key(r["src"]) in idx]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tag", required=True, help="метка кванта для имени файла, напр. Q4_K_M")
    ap.add_argument("--server", default="http://remedy:8080")
    ap.add_argument("--dir", default="/models/remedy9b")
    ap.add_argument("--sets", default=",".join(LOGS), help="главы через запятую: " + ", ".join(LOGS))
    a = ap.parse_args()
    from transformers import AutoTokenizer
    tok = AutoTokenizer.from_pretrained(f"{a.dir}/hf")
    w = np.load(f"{a.dir}/score.npy").reshape(-1).astype(np.float64)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def embed(ids):
        body = json.dumps({"content": ids, "embd_normalize": -1}).encode()
        req = urllib.request.Request(a.server + "/embeddings", body, {"Content-Type": "application/json"})
        with opener.open(req, timeout=600) as r:
            res = json.loads(r.read())
        e = res[0]["embedding"] if isinstance(res, list) else res["data"][0]["embedding"]
        e = np.asarray(e, dtype=np.float64)
        return e.reshape(-1, w.size)[-1]          # pooling last → один вектор (иногда в обёртке [[…]])

    def score(src, mt):
        msgs = [{"role": "user", "content": PROMPT.format(s="English", t="Russian", src=src)},
                {"role": "assistant", "content": mt}]
        ids = tok(tok.apply_chat_template(msgs, tokenize=False))["input_ids"]
        return float(embed(ids) @ w), len(ids)

    s1, _ = score("The cat sat on the mat.", "Кошка сидела на коврике.")
    s2, _ = score("The cat sat on the mat.", "Собака бежала по улице.")
    print(f"Проверка: верный {s1:.3f}, неверный {s2:.3f} — {'OK' if s1 > s2 else 'СТРАННО'} (на Kaggle, bf16: 6.062 и -1.969)", flush=True)

    os.makedirs("/books/.qe", exist_ok=True)
    for tag, P in ((t, pairs(t)) for t in a.sets.split(",")):
        res, t0, ntok = {}, time.time(), 0
        for side in ("4", "12"):
            out = []
            for k, (i, r, q) in enumerate(P):
                x = q if side == "4" else r
                s, n = score(x["src"], x["out"]); out.append(s); ntok += n
                if (k + 1) % 50 == 0:
                    print(f"  {tag} {side}B: {k+1}/{len(P)}, {(time.time()-t0)/(len(out) + (len(P) if side == '12' else 0)):.2f} с/абзац", flush=True)
            res[side] = out
        dt = time.time() - t0
        print(f"  {tag}: {len(P)} абзацев × 2, {dt/(2*len(P)):.2f} с/абзац, {ntok/dt:.0f} ток/с", flush=True)
        path = f"/books/.qe/qe_ReMedy-9B-{a.tag}_{tag}.jsonl"
        with open(path, "w", encoding="utf-8") as f:
            for k, (i, r, q) in enumerate(P):
                f.write(json.dumps({"set": tag, "i": i, "len": len(r["src"]), "s4": round(res["4"][k], 4), "spans4": [],
                                    "s12": round(res["12"][k], 4), "spans12": []}) + "\n")
        print("  →", path, flush=True)
    print("Готово.")


if __name__ == "__main__":
    main()
