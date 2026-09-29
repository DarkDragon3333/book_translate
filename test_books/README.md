# Тестовые книги

Книги со свободными лицензиями для проверки конвейера. В репозитории лежит только маленькая `testbook`,
Pro Git и The API Book скачиваются скриптом (≈45 МБ):

```
powershell -ExecutionPolicy Bypass -File test_books\download.ps1
```

Чтобы перевести книгу, скопируйте её в `source`.

| Файл | Книга | Лицензия | Объём | Что проверяет |
|---|---|---|---|---|
| `testbook.epub`, `testbook.pdf` | Translation Test Book (собрана для проекта, исходник в `testbook_src/`) | CC BY 4.0 | 3 главы, PDF 11 стр., перевод — несколько минут | блоки кода Java/Python/консоль, inline-код, формулы (MathML в EPUB, LaTeX в PDF), сноски, вложенные списки, таблица с числами, рисунки с подписями, список определений, примечание, нижний и верхний индекс |
| `progit.epub`, `progit.pdf` | Scott Chacon, Ben Straub. Pro Git, 2nd ed. (релиз 2.1.450) | CC BY-NC-SA 3.0 | PDF 501 стр. | длинная книга с кодом (Asciidoctor), таблицы, рисунки; в листингах есть строки с адресами `https://…` |
| `apibook.epub`, `apibook.pdf` | Sergey Konstantinov. The API Book (коммит a86cdba) | CC BY-NC 4.0 | PDF 437 стр. | проза с кодом JSON/HTTP, PDF свёрстан из HTML (Chromium) |

Источники: github.com/progit/progit2 (релизы), github.com/twirl/The-API-Book (папка `docs`).

## Шрифты кода в PDF

`scripts/pdf_analyze.py` должен найти их все — это и есть проверка:

| PDF | Шрифт кода | Как находится |
|---|---|---|
| `testbook.pdf` | DejaVuSansMono | по названию |
| `progit.pdf` | mplus1mn-regular | по одинаковой ширине символов (в названии нет Mono/Courier) |
| `apibook.pdf` | Roboto-Mono-Thin | по названию |

## Пересборка testbook

Нужны pandoc 3 и xelatex. Если в TeX Live нет пакета `lmodern`, строку `\usepackage{lmodern}` из `.tex` нужно удалить.

```
cd test_books/testbook_src
pandoc testbook.md -o ../testbook.epub --mathml --toc --split-level=1
pandoc testbook.md -s -o testbook.tex -V documentclass=book -V classoption=oneside -V geometry:a5paper,margin=18mm \
  -V fontsize=10pt -V mainfont="DejaVu Serif" -V monofont="DejaVu Sans Mono" -V monofontoptions=Scale=0.85 \
  --toc --top-level-division=chapter
xelatex testbook.tex && xelatex testbook.tex && mv testbook.pdf ..
```
