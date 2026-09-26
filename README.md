# book_translate — локальный перевод технических книг EN → RU

Перевод EPUB и PDF локальной моделью (Ollama + translategemma, GPU 4 ГБ) без облака.
Вся система поднимается одним `docker compose`.

| Формат | Инструмент | Что сохраняется |
|---|---|---|
| EPUB | [bilingual_book_maker](https://github.com/yihong0618/bilingual_book_maker) + скрипты `scripts/` | код, формулы, списки, сноски, ссылки, подписи, переменные с индексами |
| PDF  | [PDFMathTranslate-next](https://github.com/PDFMathTranslate/PDFMathTranslate-next) + проверка и ремонт страниц | вёрстка страниц; код по шрифту |

## Требования (один раз)

- Windows 11 + Docker Desktop (WSL2), драйвер NVIDIA. Проверка GPU:
  `docker run --rm --gpus all nvidia/cuda:12.4.1-base-ubuntu22.04 nvidia-smi`
- `C:\Users\<user>\.wslconfig`: `[wsl2]` / `memory=8GB` / `swap=16GB`, затем `wsl --shutdown`.
- В PowerShell писать `docker.exe` (если в `System32` лежит посторонний файл `docker`).

## Запуск

```
cd C:\book_translate
docker compose up -d
```
Первый раз скачается модель (~3,3 ГБ). Проверка: `docker compose ps -a` — `ollama` healthy,
`ollama-init` Exited (0); `docker compose logs ollama-init` → «Модель translategemma-gpu готова».

## Перевод EPUB

1. Положить книгу в `source\` (например, `geron.epub`).
2. Отключить сон ноутбука: `powercfg /change standby-timeout-ac 0` (на зарядке).
3. ```
   docker compose run --rm translate geron.epub ml,math
   ```
   Второй аргумент — области глоссария (см. ниже), по умолчанию только `base`.
4. Результат в `books\geron\`:
   - `geron.epub` — оригинал,
   - `geron_ru.epub` — перевод,
   - `geron_qa.txt` — отчёт: сверка кода, формул, списков, ссылок с оригиналом.

Если перевод оборвался — повторить ту же команду: он продолжится с места остановки.
Пробный прогон одной главы: `docker compose run --rm -e ONLY=ch04.html translate geron.epub ml,math`.
Скорость ~10 мин на главу (~3,5 ч на книгу 700 страниц).

## Перевод PDF

```
docker compose run --rm translate-pdf java.pdf programming
```
Первый запуск соберёт образ конвейера (pdf2zh + qpdf, pdfplumber — несколько минут).
Что происходит:
1. Разбор: шрифт кода определяется сам (Courier, UbuntuMono, *Mono*…), ищутся рекламные строки (t.me, телеграм).
2. Реклама вырезается из рабочей копии — с её перевода у pdf2zh начиналась «подмена кода».
3. Перевод pdf2zh (~25 с/страница, ~5 ч на 700 страниц).
4. Проверка каждой страницы против оригинала: потеря кода, русский текст внутри кода, «чужие» строки на месте кода.
5. Ремонт: плохие страницы переводятся заново короткими прогонами (по 3, затем по 1 странице),
   берётся лучшая версия; если код так и не восстановился — после страницы вставляется английский оригинал с подписью.

Результат в `books\java\`: `java.pdf`, `java_ru.pdf`, `java_qa.txt`.
Пробный прогон части книги: `docker compose run --rm -e PAGES=40-60 translate-pdf java.pdf programming`.
Оборвалось — повторить ту же команду (готовые абзацы возьмутся из кэша pdf2zh).

Веб-интерфейс pdf2zh для ручной работы: `docker compose --profile pdf up -d` → http://127.0.0.1:7860
(настройки — `config/pdf2zh/config.v3.toml`, в интерфейсе не сохранять; остановить: `docker compose --profile pdf stop pdf2zh`).

## Глоссарий

`config/glossary/*.csv`, формат pdf2zh: `source,target,tgt_lng`, одна форма — именительный падеж
(модель склоняет сама).

| Файл | Когда |
|---|---|
| `base.csv` | всегда |
| `programming.csv`, `ml.csv`, `math.csv` | по аргументу `ml,math` |
| `keep.csv` | непереводимые названия (SVD, PyTorch) — только для EPUB (в pdf2zh пары-тождества мешали) |
| `source/<книга>.glossary.csv` | словарь конкретной книги, подхватывается сам |

Одинаковые английские термины в разных областях переводятся по-разному (feature: признак / функциональность) —
поэтому области выбираются под книгу. Пополнять по итогам каждой книги.

## Структура

```
docker-compose.yml     сервисы: ollama, ollama-init, translate (EPUB, bbm), translate-pdf (PDF), pdf2zh (GUI)
docker/pdf/Dockerfile  образ PDF-конвейера: pdf2zh + qpdf, poppler-utils, pdfplumber, reportlab
config/ollama/Modelfile  translategemma-gpu: num_gpu 99, num_ctx 3072, temperature 0.2
config/pdf2zh/         config.v3.toml
config/glossary/       глоссарии
scripts/               translate.sh → epub.sh: prep → bbm → restore → normalize → qa_epub
                                   → pdf.sh:  pdf_analyze → pdf_clean → pdf2zh → repair_pdf (qa_pdf)
source/  books/        книги (в git не попадают)
```

Все образы закреплены по digest. Обновление — только осознанно: сменить digest в compose и прогнать тестовую главу.

## Проблемы

| Симптом | Что делать |
|---|---|
| `ollama ps` показывает CPU/GPU, медленно | `docker compose up -d ollama-init` (пересоздаст модель с `num_gpu 99`) |
| Docker завис, ошибка 500 | не хватило ОЗУ: проверить `.wslconfig`, `wsl --shutdown` |
| Скрипты падают с `\r: not found` | файлы получили CRLF: `git config core.autocrlf false` и заново `git checkout .` |
