#!/usr/bin/env python3
"""Чинит в GGUF тип служебных токенов (<start_of_turn> и т. п.), чтобы llama.cpp/Ollama
разбирали их в запросе как один токен, а не как куски текста.

Причина (12.10.2026, EEVE-Rosetta-7B-2602 от mradermacher): у <start_of_turn> тип NORMAL,
поэтому шаблон запроса превращался в мусор (8 токенов вместо одного) и модель зацикливалась.
Типы: 1 NORMAL, 3 CONTROL, 4 USER_DEFINED. Исправленные получают CONTROL.

Скрипт без зависимостей: читает заголовок GGUF, печатает типы, пишет исправленную копию
(исходный блоб Ollama не трогает — его имя = sha256 содержимого).

  python gguf_fix_special.py --manifest <путь к манифесту Ollama> --root /ollama --out /ollama/patched/x.gguf
"""
import argparse, json, os, shutil, struct, sys

a = argparse.ArgumentParser()
a.add_argument('--root', default='/ollama', help='том Ollama (/root/.ollama внутри контейнера ollama)')
a.add_argument('--manifest', default='models/manifests/hf.co/mradermacher/YanoljaNEXT-EEVE-Rosetta-7B-2602-GGUF/Q4_K_M')
a.add_argument('--out', default='/ollama/patched/eeve-rosetta-7b-2602-q4_k_m-fixed.gguf')
a.add_argument('--tokens', default='<start_of_turn>,<end_of_turn>,<bos>,<eos>,<pad>')
a.add_argument('--dry', action='store_true', help='только показать типы')
args = a.parse_args()

man = json.load(open(os.path.join(args.root, args.manifest)))
dig = [l['digest'] for l in man['layers'] if l['mediaType'] == 'application/vnd.ollama.image.model'][0]
src = os.path.join(args.root, 'models/blobs', dig.replace(':', '-'))
print('модель:', src, os.path.getsize(src), 'байт')

SIZES = {0: 1, 1: 1, 2: 2, 3: 2, 4: 4, 5: 4, 6: 4, 7: 1, 10: 8, 11: 8, 12: 8}
f = open(src, 'rb')
rd = lambda n: f.read(n)
u32 = lambda: struct.unpack('<I', rd(4))[0]
u64 = lambda: struct.unpack('<Q', rd(8))[0]
def sstr(): return rd(u64()).decode('utf-8', 'replace')

assert rd(4) == b'GGUF', 'не GGUF'
ver = u32(); nt = u64(); nkv = u64()
print('GGUF версия', ver, 'тензоров', nt, 'ключей', nkv)
tokens = None; types_off = None; types_n = None
for _ in range(nkv):
    key = sstr(); vt = u32()
    if vt == 8:
        sstr()
    elif vt == 9:
        it = u32(); n = u64()
        if key == 'tokenizer.ggml.tokens' and it == 8:
            tokens = [sstr() for _ in range(n)]
        elif key == 'tokenizer.ggml.token_type' and it == 5:
            types_off = f.tell(); types_n = n; f.seek(4 * n, 1)
        elif it == 8:
            for _ in range(n): sstr()
        else:
            f.seek(SIZES[it] * n, 1)
    else:
        f.seek(SIZES[vt], 1)
assert tokens and types_off, 'нет словаря или типов токенов'
want = [t for t in args.tokens.split(',') if t]
fix = []
for t in want:
    ids = [i for i, s in enumerate(tokens) if s == t]
    for i in ids:
        f.seek(types_off + 4 * i); ty = struct.unpack('<i', rd(4))[0]
        print('  %-18s id %-7d тип %d%s' % (t, i, ty, '' if ty in (3, 4) else '  ← будет CONTROL'))
        if ty not in (3, 4): fix.append(i)
    if not ids: print('  %-18s нет в словаре' % t)
f.close()
if not fix:
    print('исправлять нечего'); sys.exit(0)
if args.dry:
    print('--dry: файл не записан'); sys.exit(0)
os.makedirs(os.path.dirname(args.out), exist_ok=True)
print('копирую в', args.out, '…'); shutil.copyfile(src, args.out)
with open(args.out, 'r+b') as g:
    for i in fix:
        g.seek(types_off + 4 * i); g.write(struct.pack('<i', 3))
print('готово: исправлено токенов', len(fix))
