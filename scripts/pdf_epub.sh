#!/bin/sh
# PDF → EPUB → перевод: Docling (разметка) → docling_rebuild (английский EPUB) → EPUB-конвейер (epub.sh).
# Вызывается из translate.sh (сервис pdf-epub). Тест на части книги: -e PAGES=170-229.
# Только английский EPUB, без перевода: -e EN_ONLY=1.
# Повтор той же команды продолжает с места остановки: готовые куски разметки, английский EPUB
# и прерванный перевод берутся из books/<книга>_epub. Начать с нуля — удалить эту папку.
set -eu

SRC=$1; W=$2; NAME=$3; DOMAINS=$4
SCRIPTS=${SCRIPTS:-/scripts}
EN="$W/$NAME.epub"                   # английский EPUB = «оригинал» для epub.sh
DJ="$W/.docling.json"

log() { echo "[$(date +%H:%M:%S)] $*"; }
fail() { log "ОШИБКА: $*"; log "Служебные файлы оставлены в ${W#/}. Повтори ту же команду — готовое возьмётся из кэша."; exit 1; }

[ -n "${PAGES:-}" ] && log "    Тестовый режим: только страницы $PAGES"

if [ -f "$EN" ]; then
    log "A/B Английский EPUB уже собран — сразу к переводу (пересобрать: удалить ${EN#/} и начатый перевод)"
else
    log "A/B Разметка Docling (~1 с/стр. на CPU)"
    set -- --pdf "$SRC" --out "$DJ" --cache "$W/.docling"
    [ -n "${PAGES:-}" ] && set -- "$@" --pages "$PAGES"
    python "$SCRIPTS/docling_fetch.py" "$@" || fail "docling_fetch.py"

    log "B/B Сборка английского EPUB"
    # шрифт кода книги: по названию или по одинаковой ширине символов (mplus1mn в Pro Git и т. п.)
    python "$SCRIPTS/pdf_analyze.py" "$SRC" --fonts-only --json "$W/.fonts.json" || fail "pdf_analyze.py"
    CODE_FONT=$(python -c 'import json,sys; print(json.load(open(sys.argv[1]))["code_font_pattern"])' "$W/.fonts.json")
    set -- --pdf "$SRC" --json "$DJ" --out "$W/.en.epub" --report "$W/${NAME}_docling.txt"
    [ -n "$CODE_FONT" ] && set -- "$@" --code-font "$CODE_FONT"
    python "$SCRIPTS/docling_rebuild.py" "$@" > /dev/null || fail "docling_rebuild.py"
    mv "$W/.en.epub" "$EN"
    sed -n '1,40p' "$W/${NAME}_docling.txt"
fi

if [ -n "${EN_ONLY:-}" ]; then
    log "Готово (EN_ONLY): английский EPUB ${EN#/}, отчёт ${W#/}/${NAME}_docling.txt"
    exit 0
fi

# Дальше — обычный EPUB-конвейер: глоссарий, bbm (Rosetta через адаптер), восстановление, термины, оглавление, проверка
sh "$SCRIPTS/epub.sh" "$EN" "$W" "$NAME" "$DOMAINS"
