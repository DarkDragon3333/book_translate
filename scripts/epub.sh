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
fail() { log "ОШИБКА: $*"; log "Служебные файлы оставлены в books/$NAME. Повтори ту же команду — перевод продолжится."; exit 1; }

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
    python "$SCRIPTS/prep_epub.py" "$ORIG" "$WORKBOOK"
fi

SKIP=$(python "$SCRIPTS/prep_epub.py" --index-files "$WORKBOOK")
[ -n "$SKIP" ] && log "    Не переводятся (предметный указатель): $SKIP"

log "3/6 Перевод (bbm, модель $MODEL). Это долго: ~10 мин на главу."
set -- --book_name "$WORKBOOK" \
    --api_base "$OLLAMA_URL/v1" --model "$MODEL" \
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

log "5/6 Выравнивание терминов"
python "$SCRIPTS/normalize_terms.py" "$W/.step.epub" "$OUT" || fail "normalize_terms.py"

log "6/6 Проверка"
python "$SCRIPTS/qa_epub.py" "$ORIG" "$OUT" > "$QA" || fail "qa_epub.py"
cat "$QA"

rm -f "$WORKBOOK" "$W/work_bilingual.epub" "$W/work_bilingual_temp.epub" "$W/work_plan.json" \
      "$W/.work.temp.bin" "$W/.step.epub" "$GLOS"
log "Готово: books/$NAME/${NAME}_ru.epub, отчёт: books/$NAME/${NAME}_qa.txt"
