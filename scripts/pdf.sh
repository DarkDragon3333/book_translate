#!/bin/sh
# PDF-конвейер: разбор → очистка рекламы → pdf2zh → проверка и ремонт → отчёт.
# Вызывается из translate.sh (сервис translate-pdf). Тест на части книги: -e PAGES=40-60
set -eu

SRC=$1; W=$2; NAME=$3; DOMAINS=$4
SOURCE_DIR=${SOURCE_DIR:-/source}
SCRIPTS=${SCRIPTS:-/scripts}
CONFIG=${CONFIG:-/config}
CFG="$CONFIG/pdf2zh/config.v3.toml"

ORIG="$W/$NAME.pdf"; OUT="$W/${NAME}_ru.pdf"; QA="$W/${NAME}_qa.txt"
WORK="$W/work.pdf"; PASS0="$W/pass0.pdf"; GLOS="$W/.glossary.csv"; AN="$W/.analysis.json"

# Регулярка шрифта кода: из разбора книги, а если не найден — из config.v3.toml
code_pattern() {
    python - "$AN" "$CFG" <<'PY'
import json, sys, tomllib
p = json.load(open(sys.argv[1]))['code_font_pattern']
print(p or tomllib.load(open(sys.argv[2], 'rb'))['pdf']['formular_font_pattern'])
PY
}
log() { echo "[$(date +%H:%M:%S)] $*"; }
fail() { log "ОШИБКА: $*"; log "Служебные файлы оставлены в books/$NAME. Повтори ту же команду — готовое возьмётся из кэша."; exit 1; }

[ -f "$ORIG" ] || cp "$SRC" "$ORIG"

if [ -f "$PASS0" ] && [ -f "$WORK" ]; then
    log "1-4/6 Основной перевод уже есть — сразу к проверке"
else
    BASE="$ORIG"
    if [ -n "${PAGES:-}" ]; then
        log "    Тестовый режим: только страницы $PAGES"
        qpdf "$ORIG" --pages "$ORIG" "$PAGES" -- "$W/.subset.pdf" || fail "неверный PAGES=$PAGES"
        BASE="$W/.subset.pdf"
    fi
    log "1/6 Разбор PDF"
    python "$SCRIPTS/pdf_analyze.py" "$BASE" --json "$AN"
    log "2/6 Очистка рекламы"
    python "$SCRIPTS/pdf_clean.py" "$BASE" "$WORK" "$AN"
    log "3/6 Глоссарий: $DOMAINS"
    python "$SCRIPTS/build_glossary.py" --format pdf2zh --dir "$CONFIG/glossary" --domains "$DOMAINS" \
        --extra "$SOURCE_DIR/$NAME.glossary.csv" --out "$GLOS"
    PAT=$(code_pattern)
    log "4/6 Перевод pdf2zh (~25 с/стр.). Шрифт кода: $PAT"
    rm -rf "$W/.pass0"
    set -- --config-file "$CFG" "$WORK" --output "$W/.pass0" --glossaries "$GLOS"
    [ -n "$PAT" ] && set -- "$@" --formular-font-pattern "$PAT"
    pdf2zh_next "$@" || fail "pdf2zh завершился с ошибкой"
    RES=$(ls -t "$W"/.pass0/*mono*.pdf 2>/dev/null | head -1 || true)
    [ -n "$RES" ] || RES=$(ls -t "$W"/.pass0/*.pdf 2>/dev/null | head -1 || true)
    [ -n "$RES" ] || fail "pdf2zh не создал перевод"
    mv "$RES" "$PASS0"
fi

PAT=$(code_pattern)
log "5/6 Проверка и ремонт страниц"
python "$SCRIPTS/repair_pdf.py" --orig "$WORK" --tran "$PASS0" --out "$OUT" --code-font "$PAT" \
    --config "$CFG" --glossary "$GLOS" --workdir "$W/.repair" --report "$QA" || fail "repair_pdf.py"

log "6/6 Отчёт: books/$NAME/${NAME}_qa.txt"
python - "$AN" >> "$QA" <<'PY'
import json, sys
a = json.load(open(sys.argv[1]))
print('\nРазбор исходника: страниц %d, шрифт кода: %s' % (a['pages'], ', '.join(f for f, _ in a['code_fonts']) or 'не найден'))
for l, p in a['ads'].items():
    print('  реклама (вырезана из рабочей копии): стр. %s: %s' % (p, l[:80]))
PY
rm -rf "$W/.pass0" "$W/.repair" "$W/.subset.pdf" "$WORK" "$PASS0" "$GLOS" "$AN"
log "Готово: books/$NAME/${NAME}_ru.pdf"
