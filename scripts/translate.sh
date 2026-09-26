#!/bin/sh
# Точка входа конвейера:
#   docker compose run --rm translate     <книга.epub> [области глоссария]
#   docker compose run --rm translate-pdf <книга.pdf>  [области глоссария]
# Области: base (всегда), programming, ml, math — через запятую, например ml,math.
# Тест: EPUB — одна глава -e ONLY=ch04.html; PDF — диапазон страниц -e PAGES=40-60
set -eu

SOURCE_DIR=${SOURCE_DIR:-/source}
BOOKS_DIR=${BOOKS_DIR:-/books}
SCRIPTS=${SCRIPTS:-/scripts}

if [ $# -lt 1 ]; then
    echo "Использование: docker compose run --rm translate <книга.epub> [области]"
    echo "               docker compose run --rm translate-pdf <книга.pdf> [области]"
    echo "Книги в папке source:"
    ls -1 "$SOURCE_DIR" 2>/dev/null | grep -iE '\.(epub|pdf)$' | sed 's/^/  /' || true
    exit 1
fi

FILE=$1
DOMAINS=${2:-base}
SRC="$SOURCE_DIR/$FILE"
[ -f "$SRC" ] || { echo "Нет файла source/$FILE"; exit 1; }

NAME=${FILE%.*}
EXT=$(printf '%s' "${FILE##*.}" | tr 'A-Z' 'a-z')
WORK="$BOOKS_DIR/$NAME"
mkdir -p "$WORK"

case "$EXT" in
    epub) command -v pdf2zh_next >/dev/null 2>&1 && {
              echo "EPUB переводит сервис translate: docker compose run --rm translate $FILE $DOMAINS"; exit 1; }
          exec sh "$SCRIPTS/epub.sh" "$SRC" "$WORK" "$NAME" "$DOMAINS" ;;
    pdf)  command -v pdf2zh_next >/dev/null 2>&1 || {
              echo "PDF переводит сервис translate-pdf: docker compose run --rm translate-pdf $FILE $DOMAINS"; exit 1; }
          exec sh "$SCRIPTS/pdf.sh" "$SRC" "$WORK" "$NAME" "$DOMAINS" ;;
    *)    echo "Неизвестный формат: .$EXT (нужен .epub или .pdf)"; exit 1 ;;
esac
