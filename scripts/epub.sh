#!/bin/sh
# EPUB-конвейер: подготовка → bbm → восстановление → термины → проверка.
# Вызывается из translate.sh. Повторный запуск после обрыва продолжает перевод.
# Гибридный режим (MODEL=hybrid, HYBRID_SHARE=40): проход 1 — быстрая модель (rosetta-ru) переводит всё;
# проход 2 — отбор абзацев (сбои адаптера, ru_check, оценка ReMedy) на HYBRID_SHARE % текста;
# проход 3 — bbm заново: отобранные абзацы переводит сильная модель (rosetta12-ru), остальные адаптер
# берёт из журнала прохода 1 мгновенно. Этап — в .hybrid_phase (fast | score | strong), обрыв продолжается с него.
set -eu

SRC=$1; W=$2; NAME=$3; DOMAINS=$4
SOURCE_DIR=${SOURCE_DIR:-/source}
SCRIPTS=${SCRIPTS:-/scripts}
CONFIG=${CONFIG:-/config}
BBM=${BBM:-/app/make_book.py}
OLLAMA_URL=${OLLAMA_URL:-http://ollama:11434}
MODEL=${MODEL:-rosetta-ru}
RUNMODEL=$MODEL                 # что записано в .model: модель или hybrid
HYBRID=""
if [ "$MODEL" = hybrid ]; then
    HYBRID=1
    FAST_MODEL=${HYBRID_FAST:-rosetta-ru}
    STRONG_MODEL=${HYBRID_STRONG:-rosetta12-ru}
    SHARE=${HYBRID_SHARE:-40}
    case "$SHARE" in ''|*[!0-9]*) echo "HYBRID_SHARE — целое число процентов (1–90), а не «$SHARE»"; exit 1 ;; esac
    [ "$SHARE" -ge 1 ] && [ "$SHARE" -le 90 ] || { echo "HYBRID_SHARE — от 1 до 90 процентов, а не $SHARE"; exit 1; }
    MODEL=$FAST_MODEL
fi

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
    --extra "$SOURCE_DIR/$NAME.glossary.csv" --book "$ORIG" --out "$GLOS"

RESUME=""
PHASE=""
if { [ -f "$W/.work.temp.bin" ] || [ -f "$W/.hybrid_phase" ]; } && [ -f "$WORKBOOK" ]; then
    # bbm продолжает только той же моделью (отпечаток в .work.temp.bin) — объясняем по-человечески заранее
    PREV=$(cat "$W/.model" 2>/dev/null || true)
    if [ -n "$PREV" ] && [ "$PREV" != "$RUNMODEL" ]; then
        log "ОШИБКА: прерванный перевод начат моделью $PREV, а сейчас выбрана $RUNMODEL."
        log "Продолжить можно только той же моделью (-e MODEL=$PREV). Начать заново моделью $RUNMODEL — удали папку ${W#/}."
        exit 1
    fi
    [ -f "$W/.work.temp.bin" ] && RESUME="--resume"
    [ -n "$HYBRID" ] && PHASE=$(cat "$W/.hybrid_phase" 2>/dev/null || echo fast)
    log "2/6 Найден прерванный перевод — продолжаю с места остановки"
else
    log "2/6 Подготовка книги"
    rm -f "$W/.rosetta_log.jsonl" "$W/.senses.json" "$W/.keep.json"   # журнал адаптера, карты смыслов и кода — только для этого перевода
    rm -f "$W/.hybrid_phase" "$W/.hybrid_fast.jsonl" "$W/.hybrid_qe.jsonl" "$W/.hybrid_pick.json" "$W/.hybrid_done"
    export ITALICS="${ITALICS:-1}"            # курсив в прозе передаётся модели (с 14.10 по умолчанию; выкл.: -e ITALICS=0)
    [ "$ITALICS" = 1 ] || log "    Курсив в прозе модели не передаётся (ITALICS=0)"
    python "$SCRIPTS/prep_epub.py" "$ORIG" "$WORKBOOK" --keep-map "$W/.keep.json"
fi
printf '%s\n' "$RUNMODEL" > "$W/.model"  # модель этого перевода: интерфейс и повторный запуск сверяются с ней
if [ -n "$HYBRID" ]; then
    [ -n "$PHASE" ] || PHASE=fast
    printf '%s\n' "$PHASE" > "$W/.hybrid_phase"
fi

SKIP=$(python "$SCRIPTS/prep_epub.py" --index-files "$WORKBOOK")
[ -n "$SKIP" ] && log "    Не переводятся (предметный указатель): $SKIP"

# Rosetta понимает только свой формат запроса → между bbm и Ollama встаёт адаптер (scripts/rosetta_proxy.py):
# передаёт глоссарий в инструкцию модели, чинит метки кода, отвечает bbm в его формате. Журнал — .rosetta_log.jsonl.
PROXY_PID=""
stop_proxy() {
    [ -n "$PROXY_PID" ] && kill "$PROXY_PID" 2>/dev/null || true
    [ -n "$PROXY_PID" ] && wait "$PROXY_PID" 2>/dev/null || true
    PROXY_PID=""
}
trap 'stop_proxy' EXIT

# start_proxy МОДЕЛЬ [доп. аргументы адаптера] → API_URL
start_proxy() {
    PM=$1; shift
    API_URL="$OLLAMA_URL"
    case "$PM" in
        rosetta*)
            # смысл многозначных терминов по контексту (feature, transformer, recall…) — карта по всей книге до перевода
            if [ ! -f "$W/.senses.json" ] && [ -f "$CONFIG/senses.csv" ]; then
                python "$SCRIPTS/senses.py" "$WORKBOOK" "$W/.senses.json" --rules "$CONFIG/senses.csv" \
                    --glossary-dir "$CONFIG/glossary" --domains "$DOMAINS" || log "    Карта смыслов не построена — термины как в глоссарии"
            fi
            [ -f "$W/.senses.json" ] && set -- "$@" --senses "$W/.senses.json" --senses-rules "$CONFIG/senses.csv"
            [ -s "$W/.keep.json" ] && set -- "$@" --keep "$W/.keep.json"      # код в заголовках (switch, this()…) — не переводить
            [ -s "$GLOS" ] && set -- "$@" --glossary-file "$GLOS"                 # термины с буквой-переменной: ⟦var⟧-means
            [ "${TOPIC_CONTEXT:-1}" = 1 ] && set -- "$@" --domains "$DOMAINS"     # Context по темам глоссариев книги (с 14.10 по умолчанию; выкл.: -e TOPIC_CONTEXT=0)
            [ "${LOGPROBS:-0}" = 1 ] && set -- "$@" --logprobs                     # тест 13.10: уверенность модели — в журнал
            python "$SCRIPTS/rosetta_proxy.py" --listen 127.0.0.1:8083 --target "$OLLAMA_URL" --log "$W/.rosetta_log.jsonl" "$@" &
            PROXY_PID=$!
            for i in 1 2 3 4 5 6 7 8 9 10; do
                python -c "import urllib.request as u; u.build_opener(u.ProxyHandler({})).open('http://127.0.0.1:8083/api/tags', timeout=2)" \
                    2>/dev/null && break
                sleep 1
            done
            API_URL="http://127.0.0.1:8083"
            log "    Адаптер модели $PM: $API_URL → $OLLAMA_URL" ;;
    esac
}

# run_bbm МОДЕЛЬ — перевод книги (с продолжением, если есть .work.temp.bin)
run_bbm() {
    set -- --book_name "$WORKBOOK" \
        --api_base "$API_URL/v1" --model "$1" \
        --language "ru:Russian" --single_translate --no_disclosure \
        --plan-classify all --accumulated_num 1 --temperature "${TEMPERATURE:-0.2}" \
        --exclude-translate-tags "sup,sub,var,code,pre,math,a"
    [ -s "$GLOS" ] && set -- "$@" --glossary "$GLOS"
    [ -f "$W/.work.temp.bin" ] && set -- "$@" --resume
    if [ -n "${ONLY:-}" ]; then
        set -- "$@" --only_filelist "$ONLY"; log "    Тестовый режим: только $ONLY"
    elif [ -n "$SKIP" ]; then
        set -- "$@" --exclude_filelist "$SKIP"
    fi
    python "$BBM" "$@" || fail "bbm завершился с ошибкой"
    [ -f "$W/work_bilingual.epub" ] || fail "bbm не создал work_bilingual.epub"
}

case "$MODEL" in
    rosetta12*) PACE="~1 ч на главу (режим «качество», часть модели в ОЗУ)" ;;
    *)          PACE="~10 мин на главу" ;;
esac
[ -n "${TEMPERATURE:-}" ] && log "    Температура перевода: $TEMPERATURE (по умолчанию 0.2)"
if [ -z "$HYBRID" ]; then
    log "3/6 Перевод (bbm, модель $MODEL). Это долго: $PACE."
    start_proxy "$MODEL"
    run_bbm "$MODEL"
else
    log "    Гибрид: $FAST_MODEL переводит всё, затем $STRONG_MODEL заново переводит худшие абзацы — $SHARE% текста"
    # всё нужное для трёх проходов проверяем сразу, а не через несколько часов
    python -c "
import json, sys, urllib.request as u
o = u.build_opener(u.ProxyHandler({}))
get = lambda url: json.loads(o.open(url, timeout=10).read())
have = {m['name'].split(':latest')[0] for m in get(sys.argv[1] + '/api/tags')['models']}
miss = [m for m in sys.argv[3:] if m not in have]
if miss:
    sys.exit('нет модели Ollama: ' + ', '.join(miss) + ' (режим «качество» — README, раздел «Режим качество»)')
if sys.argv[2] != 'score-done':
    try:
        st = get(sys.argv[2] + '/status')
    except Exception as e:
        sys.exit('сервис оценки ReMedy не отвечает (%s): docker compose up -d remedy' % e)
    if not st.get('available'):
        sys.exit('нет модели ReMedy (%s) — README, раздел «Гибридный режим»' % st.get('model'))
" "$OLLAMA_URL" "$([ "$PHASE" = strong ] && echo score-done || echo "${REMEDY_URL:-http://remedy:8091}")" \
        "$FAST_MODEL" "$STRONG_MODEL" || fail "гибридный режим не готов"
    if [ "$PHASE" = fast ]; then
        log "3/6 Перевод (гибрид 1/3: быстрая модель $FAST_MODEL, $PACE)"
        start_proxy "$FAST_MODEL"
        run_bbm "$FAST_MODEL"
        stop_proxy
        mv -f "$W/.rosetta_log.jsonl" "$W/.hybrid_fast.jsonl"
        rm -f "$W/work_bilingual.epub" "$W/work_bilingual_temp.epub" "$W/work_plan.json" "$W/.work.temp.bin" \
              "$W/.hybrid_qe.jsonl" "$W/.hybrid_pick.json"
        PHASE=score; echo score > "$W/.hybrid_phase"
    fi
    if [ "$PHASE" = strong ] && [ -f "$W/.hybrid_pick.json" ]; then
        PICKED=$(python -c "import json,sys; print('%g' % json.load(open(sys.argv[1], encoding='utf-8'))['share'])" "$W/.hybrid_pick.json")
        [ "$PICKED" = "$SHARE" ] || log "    Абзацы уже отобраны на $PICKED% текста — продолжаю с ними (новая доля действует для перевода с нуля)"
    fi
    if [ "$PHASE" = score ]; then
        log "3/6 Перевод (гибрид 2/3: отбор абзацев — сбои адаптера, ru_check, оценка ReMedy)"
        python "$SCRIPTS/hybrid_pick.py" "$W/.hybrid_fast.jsonl" --share "$SHARE" --qe "$W/.hybrid_qe.jsonl" \
            --out "$W/.hybrid_pick.json" --words "$SCRIPTS/tools/ru_words.txt.gz" \
            --corpus "$CONFIG/ru_corpus_words.txt.gz" --glossary-dir "$CONFIG/glossary" --ollama "$OLLAMA_URL" \
            || fail "отбор абзацев для гибрида (hybrid_pick.py)"
        rm -f "$W/.rosetta_log.jsonl" "$W/.hybrid_done"
        PHASE=strong; echo strong > "$W/.hybrid_phase"
    fi
    log "3/6 Перевод (гибрид 3/3: $STRONG_MODEL для отобранных абзацев, ~1 ч на главу целиком — здесь только их доля)"
    log "    Шкала bbm ниже считает все абзацы: неотобранные берутся из прохода 1 мгновенно, поэтому она идёт рывками."
    log "    Сколько осталось модели «качество» — строки «[гибрид 3/3] … N из M отобранных»."
    start_proxy "$STRONG_MODEL" --replay "$W/.hybrid_fast.jsonl" --pick "$W/.hybrid_pick.json" --done "$W/.hybrid_done"
    run_bbm "$STRONG_MODEL"
    stop_proxy
fi

log "4/6 Восстановление разметки"
python "$SCRIPTS/restore_epub.py" "$W/work_bilingual.epub" "$W/.step.epub" || fail "restore_epub.py"

log "5/6 Выравнивание терминов и оглавление"
python "$SCRIPTS/normalize_terms.py" "$W/.step.epub" "$OUT" || fail "normalize_terms.py"
python "$SCRIPTS/fix_nav.py" "$OUT" || fail "fix_nav.py"

log "6/6 Проверка"
python "$SCRIPTS/qa_epub.py" "$ORIG" "$OUT" ${ONLY:+--only "$ONLY"} > "$QA" || fail "qa_epub.py"
if [ -f "$W/.rosetta_log.jsonl" ]; then
    python -c "
import json, sys
L = [json.loads(l) for l in open(sys.argv[1], encoding='utf-8')]
bad = sum(not r['marks_ok'] for r in L); rep = sum(r['retries'] for r in L); nj = sum(not r['json'] for r in L)
print('\nАдаптер Rosetta: абзацев %d, повторов %d, метки не сошлись %d, ответ не JSON %d' % (len(L), rep, bad, nj))
alt = sum(1 for r in L for c in r.get('senses', []) if c[1] == 'alt'); no = sum(1 for r in L for c in r.get('senses', []) if c[1] == 'none')
miss = sum(1 for r in L if r.get('senses_missing'))
if alt or no or miss:
    print('Смысл по контексту: другой перевод %d, не подсказано %d, абзац не найден в карте %d' % (alt, no, miss))
" "$W/.rosetta_log.jsonl" >> "$QA"
fi
if [ -n "$HYBRID" ] && [ -f "$W/.hybrid_pick.json" ]; then
    python -c "
import json, sys
p = json.load(open(sys.argv[1], encoding='utf-8'))
L = [json.loads(l) for l in open(sys.argv[2], encoding='utf-8') if l.strip()]
st = sum(r.get('hybrid') == 'strong' for r in L)
w = p['why']
print('Гибрид (%g%% текста на %s): отобрано абзацев %d из %d (%.1f%% текста: метки/JSON %d, ru_check %d, ReMedy %d), переведено сильной моделью %d' % (
    p['share'], sys.argv[4], p['n'], p['paragraphs'], p['pct'], w['broken'], w['ru_check'], w['remedy'], st))
" "$W/.hybrid_pick.json" "$W/.rosetta_log.jsonl" "$SHARE" "$STRONG_MODEL" >> "$QA" || true
fi
python "$SCRIPTS/ru_check.py" "$OUT" --words "$SCRIPTS/tools/ru_words.txt.gz" --corpus "$CONFIG/ru_corpus_words.txt.gz" \
    --glossary-dir "$CONFIG/glossary" ${ONLY:+--only "$ONLY"} --json "$W/.ru_check.json" >> "$QA" \
    || log "    Проверка русских слов не выполнена"
if [ -f "$W/.rosetta_log.jsonl" ]; then         # перевёрнутый смысл: отрицания и сравнения (журнал адаптера)
    python "$SCRIPTS/polarity_check.py" "$W/.rosetta_log.jsonl" --json "$W/.polarity.json" >> "$QA" \
        || log "    Проверка полярности не выполнена"
fi
cat "$QA"

rm -f "$WORKBOOK" "$W/work_bilingual.epub" "$W/work_bilingual_temp.epub" "$W/work_plan.json" \
      "$W/.work.temp.bin" "$W/.step.epub" "$GLOS" "$W/.hybrid_phase" "$W/.hybrid_done"
log "Готово: ${W#/}/${NAME}_ru.epub, отчёт: ${W#/}/${NAME}_qa.txt"
