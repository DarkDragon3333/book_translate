#!/bin/sh
# Точка входа конвейера:
#   docker compose run --rm translate     <книга.epub> [области глоссария]
#   docker compose run --rm pdf-epub      <книга.pdf>  [области глоссария]   PDF → EPUB через Docling (основной путь)
#   docker compose run --rm translate-pdf <книга.pdf>  [области глоссария]   PDF → PDF через pdf2zh
# Области: base (всегда), programming, ml, math — через запятую, например ml,math.
# Тест: EPUB — одна глава -e ONLY=ch04.html; PDF — диапазон страниц -e PAGES=40-60
set -eu

SOURCE_DIR=${SOURCE_DIR:-/source}
BOOKS_DIR=${BOOKS_DIR:-/books}
SCRIPTS=${SCRIPTS:-/scripts}

if [ $# -lt 1 ]; then
    echo "Использование: docker compose run --rm translate <книга.epub> [области]"
    echo "               docker compose run --rm pdf-epub <книга.pdf> [области]      (PDF → EPUB через Docling)"
    echo "               docker compose run --rm translate-pdf <книга.pdf> [области] (PDF → PDF через pdf2zh)"
    echo "Книги в папке source:"
    ls -1 "$SOURCE_DIR" 2>/dev/null | grep -iE '\.(epub|pdf)$' | sed 's/^/  /' || true
    exit 1
fi

FILE=$1
DOMAINS=${2:-base}
SRC="$SOURCE_DIR/$FILE"
[ -f "$SRC" ] || { echo "Нет файла source/$FILE"; exit 1; }

NAME=${FILE%.*}

# Одна книга — один перевод: вторая команда (или интерфейс) для той же книги не запустится.
# Блокировка снимается сама, когда процесс завершается (в том числе при обрыве).
lock_book() {
    mkdir -p "$1"
    command -v flock >/dev/null 2>&1 || return 0
    exec 9>"$1/.lock"
    flock -n 9 || { echo "Книга $FILE уже переводится (другая команда или интерфейс). Дождись окончания."; exit 1; }
}
EXT=$(printf '%s' "${FILE##*.}" | tr 'A-Z' 'a-z')
WORK="$BOOKS_DIR/$NAME"

case "$EXT" in
    epub) command -v pdf2zh_next >/dev/null 2>&1 && {
              echo "EPUB переводит сервис translate: docker compose run --rm translate $FILE $DOMAINS"; exit 1; }
          lock_book "$WORK"
          exec sh "$SCRIPTS/epub.sh" "$SRC" "$WORK" "$NAME" "$DOMAINS" ;;
    pdf)  if [ "${PIPELINE:-}" = docling ]; then
              # свой каталог: не мешает PDF-переводу pdf2zh той же книги; тест PAGES — отдельно от полной книги
              WORK="$BOOKS_DIR/${NAME}_epub${PAGES:+_p$PAGES}"
              lock_book "$WORK"
              exec sh "$SCRIPTS/pdf_epub.sh" "$SRC" "$WORK" "$NAME" "$DOMAINS"
          fi
          command -v pdf2zh_next >/dev/null 2>&1 || {
              echo "PDF переводят сервисы pdf-epub (через Docling в EPUB) и translate-pdf (pdf2zh в PDF):"
              echo "  docker compose run --rm pdf-epub $FILE $DOMAINS"; exit 1; }
          lock_book "$WORK"
          exec sh "$SCRIPTS/pdf.sh" "$SRC" "$WORK" "$NAME" "$DOMAINS" ;;
    *)    echo "Неизвестный формат: .$EXT (нужен .epub или .pdf)"; exit 1 ;;
esac
