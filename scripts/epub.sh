#!/bin/sh
# EPUB-конвейер: подготовка → bbm → восстановление → термины → проверка.
# Вызывается из translate.sh. Повторный запуск после обрыва продолжает перевод.
set -eu

SRC=$1; W=$2; NAME=$3; DOMAINS=$4
SOURCE_DIR=${SOURCE_DIR:-/source}
SCRIPTS=${SCRIPTS:-/scripts}
CONFIG=${CONFIG:-/config}
BBM=${BBM:-/app/make_book.py}
OLLAMA_URL=${OLLAMA_URL:-http://ollama:11434}
MODEL=${MODEL:-translategemma-gpu}

ORIG="$W/$NAME.epub"          # копия оригинала (итоговый файл 1)
OUT="$W/${NAME}_ru.epub"      # перевод (итоговый файл 2)
QA="$W/${NAME}_qa.txt"        # отчёт (итоговый файл 3)
WORKBOOK="$W/work.epub"       # служебные файлы bbm называет по имени этой книги
GLOS="$W/.glossary.txt"

log() { echo "[$(date +%H:%M:%S)] $*"; }
fail() { log "ОШИБКА: $*"; log "Служебные файлы оставлены в ${W#/}. Повтори ту же команду — перевод продолжится."; exit 1; }

[ -f "$ORIG" ] || cp "$SRC" "$ORIG"

log "1/6 Глоссарий: $DOMAINS"
python "$SCRIPTS/build_glossary.py" --format bbm --dir "$CONFIG/glossary" --domains "$DOMAINS" \
    --extra "$SOURCE_DIR/$NAME.glossary.csv" --out "$GLOS"

RESUME=""
if [ -f "$W/.work.temp.bin" ] && [ -f "$WORKBOOK" ]; then
    RESUME="--resume"
    log "2/6 Найден прерванный перевод — продолжаю с места остановки"
else
    log "2/6 Подготовка книги"
    rm -f "$W/.rosetta_log.jsonl"         # журнал адаптера — только для этого перевода
    python "$SCRIPTS/prep_epub.py" "$ORIG" "$WORKBOOK"
fi

SKIP=$(python "$SCRIPTS/prep_epub.py" --index-files "$WORKBOOK")
[ -n "$SKIP" ] && log "    Не переводятся (предметный указатель): $SKIP"

# Rosetta понимает только свой формат запроса → между bbm и Ollama встаёт адаптер (scripts/rosetta_proxy.py):
# передаёт глоссарий в её инструкцию, чинит метки кода, отвечает bbm в его формате. Журнал — .rosetta_log.jsonl.
API_URL="$OLLAMA_URL"
case "$MODEL" in
    rosetta*)
        python "$SCRIPTS/rosetta_proxy.py" --listen 127.0.0.1:8083 --target "$OLLAMA_URL" --log "$W/.rosetta_log.jsonl" &
        PROXY_PID=$!
        trap 'kill $PROXY_PID 2>/dev/null || true' EXIT
        for i in 1 2 3 4 5 6 7 8 9 10; do
            python -c "import urllib.request as u; u.build_opener(u.ProxyHandler({})).open('http://127.0.0.1:8083/api/tags', timeout=2)" \
                2>/dev/null && break
            sleep 1
        done
        API_URL="http://127.0.0.1:8083"
        log "    Адаптер Rosetta: $API_URL → $OLLAMA_URL" ;;
esac

log "3/6 Перевод (bbm, модель $MODEL). Это долго: ~10 мин на главу."
set -- --book_name "$WORKBOOK" \
    --api_base "$API_URL/v1" --model "$MODEL" \
    --language "ru:Russian" --single_translate --no_disclosure \
    --plan-classify all --accumulated_num 1 --temperature 0.2 \
    --exclude-translate-tags "sup,sub,var,code,pre,math,a"
[ -s "$GLOS" ] && set -- "$@" --glossary "$GLOS"
[ -n "$RESUME" ] && set -- "$@" "$RESUME"
if [ -n "${ONLY:-}" ]; then
    set -- "$@" --only_filelist "$ONLY"; log "    Тестовый режим: только $ONLY"
elif [ -n "$SKIP" ]; then
    set -- "$@" --exclude_filelist "$SKIP"
fi
python "$BBM" "$@" || fail "bbm завершился с ошибкой"
[ -f "$W/work_bilingual.epub" ] || fail "bbm не создал work_bilingual.epub"

log "4/6 Восстановление разметки"
python "$SCRIPTS/restore_epub.py" "$W/work_bilingual.epub" "$W/.step.epub" || fail "restore_epub.py"

log "5/6 Выравнивание терминов и оглавление"
python "$SCRIPTS/normalize_terms.py" "$W/.step.epub" "$OUT" || fail "normalize_terms.py"
python "$SCRIPTS/fix_nav.py" "$OUT" || fail "fix_nav.py"

log "6/6 Проверка"
python "$SCRIPTS/qa_epub.py" "$ORIG" "$OUT" > "$QA" || fail "qa_epub.py"
if [ -f "$W/.rosetta_log.jsonl" ]; then
    python -c "
import json, sys
L = [json.loads(l) for l in open(sys.argv[1], encoding='utf-8')]
bad = sum(not r['marks_ok'] for r in L); rep = sum(r['retries'] for r in L); nj = sum(not r['json'] for r in L)
print('\nАдаптер Rosetta: абзацев %d, повторов %d, метки не сошлись %d, ответ не JSON %d' % (len(L), rep, bad, nj))
" "$W/.rosetta_log.jsonl" >> "$QA"
fi
cat "$QA"

rm -f "$WORKBOOK" "$W/work_bilingual.epub" "$W/work_bilingual_temp.epub" "$W/work_plan.json" \
      "$W/.work.temp.bin" "$W/.step.epub" "$GLOS"
log "Готово: ${W#/}/${NAME}_ru.epub, отчёт: ${W#/}/${NAME}_qa.txt"
