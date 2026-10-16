r"""ReMedy-9B-22 → подготовка к llama.cpp (запуск в образе bt-xcl:1, том ./models → /models).

1. Скачивает ShaomuTan/ReMedy-9B-22 (18 ГБ) в /models/remedy9b/hf.
2. Берёт tokenizer.model у ReMedy-2B: у 9B его нет, а конвертеру llama.cpp для Gemma-2 нужен sentencepiece;
   словарь Gemma-2 у 2B и 9B один — проверяется ниже по совпадению токенов с tokenizer.json 9B.
3. config.json → Gemma2ForCausalLM, 41 слой (в чекпойнте слоёв 0–40; config говорит 42).
4. Вынимает голову-оценщик score.weight [1×3584] в /models/remedy9b/score.npy и убирает её из шарда:
   конвертер не знает этот тензор. Оценка = выход последнего токена (после финальной нормы) · score.
Повторный запуск безопасен: уже сделанные шаги пропускаются.
"""
import json, os, shutil, sys
import numpy as np

D = "/models/remedy9b"
HF = f"{D}/hf"


def main():
    from huggingface_hub import snapshot_download, hf_hub_download
    os.makedirs(HF, exist_ok=True)
    print("1/4 Скачиваю ShaomuTan/ReMedy-9B-22 (18 ГБ; при обрыве — запусти снова, докачает)…", flush=True)
    snapshot_download("ShaomuTan/ReMedy-9B-22", local_dir=HF)
    if not os.path.exists(f"{HF}/tokenizer.model"):
        hf_hub_download("ShaomuTan/ReMedy-2B", "tokenizer.model", local_dir=HF)

    print("2/4 Проверяю словарь tokenizer.model против tokenizer.json…", flush=True)
    import sentencepiece as spm
    from transformers import AutoTokenizer
    sp = spm.SentencePieceProcessor(model_file=f"{HF}/tokenizer.model")
    tok = AutoTokenizer.from_pretrained(HF)
    probe = "Translate the following English text: the ⟦code1⟧ property, JAR-файл и образ контейнера Kubernetes."
    a = sp.encode(probe)
    b = tok(probe, add_special_tokens=False)["input_ids"]
    print(f"    словарь: sentencepiece {sp.get_piece_size()}, tokenizer.json {len(tok)}; токены совпадают: {a == b}", flush=True)
    if a != b:
        sys.exit("ОШИБКА: словари различаются — пришли этот вывод")

    print("3/4 config.json → Gemma2ForCausalLM, 41 слой", flush=True)
    cfg_p = f"{HF}/config.json"
    if not os.path.exists(f"{HF}/config.orig.json"):
        shutil.copy(cfg_p, f"{HF}/config.orig.json")
    cfg = json.load(open(f"{HF}/config.orig.json"))
    cfg["architectures"] = ["Gemma2ForCausalLM"]
    cfg["num_hidden_layers"] = 41
    json.dump(cfg, open(cfg_p, "w"), indent=2)

    print("4/4 Вынимаю score.weight", flush=True)
    idx_p = f"{HF}/model.safetensors.index.json"
    idx = json.load(open(idx_p))
    if "score.weight" in idx["weight_map"]:
        from safetensors import safe_open
        from safetensors.torch import save_file
        shard = f"{HF}/{idx['weight_map']['score.weight']}"
        with safe_open(shard, "pt") as f:
            meta = f.metadata() or {"format": "pt"}
            tensors = {k: f.get_tensor(k) for k in f.keys()}
        w = tensors.pop("score.weight").float().numpy()
        np.save(f"{D}/score.npy", w)
        save_file(tensors, shard + ".tmp", metadata=meta)
        os.replace(shard + ".tmp", shard)
        del idx["weight_map"]["score.weight"]
        json.dump(idx, open(idx_p, "w"), indent=2)
        print(f"    score.npy: форма {w.shape}, норма {np.linalg.norm(w):.3f}", flush=True)
    else:
        print("    уже вынут", flush=True)
    layers = sorted({int(k.split('.')[2]) for k in idx["weight_map"] if k.startswith("model.layers.")})
    print(f"Готово. Слоёв в весах: {len(layers)} (0–{layers[-1]}), в config: {cfg['num_hidden_layers']}")


if __name__ == "__main__":
    main()
