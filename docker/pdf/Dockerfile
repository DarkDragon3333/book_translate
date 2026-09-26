# PDF-конвейер: pdf2zh (PDFMathTranslate-next, закреплённая версия) + инструменты проверки и ремонта.
FROM awwaawwa/pdfmathtranslate-next@sha256:c737d5342c9220a56026733f3a42182581bb4d8e5052b133e3326babffea109a

RUN apt-get update \
 && apt-get install -y --no-install-recommends qpdf poppler-utils fonts-dejavu-core \
 && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir pdfplumber==0.11.9 reportlab==4.4.10

ENTRYPOINT []
