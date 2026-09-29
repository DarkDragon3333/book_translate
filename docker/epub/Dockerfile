# PDF → EPUB через Docling: образ bbm (та же закреплённая версия, что у сервиса translate) + pdfplumber
# для docling_rebuild.py (текст, шрифты и координаты символов из PDF, рисунки). Сам Docling — отдельный сервис.
FROM ghcr.io/yihong0618/bilingual_book_maker@sha256:7f9cf02dae3d3bc4144723fcba759cf002649546af1e21cd4879fa88694ba2e6

# образ bbm работает от пользователя bbm (1000:1000) без домашней папки — ставим пакеты от root в системный Python
USER root
RUN pip install --no-cache-dir pdfplumber==0.11.9
USER 1000:1000

ENTRYPOINT []
