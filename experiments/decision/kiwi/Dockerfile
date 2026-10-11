# CometKiwi (Unbabel/wmt22-cometkiwi-da) на CPU для проверки перевода.
# Сборка: docker.exe build -t bt-kiwi:1 experiments\decision\kiwi
FROM python:3.11-slim
RUN pip install --no-cache-dir torch==2.4.1 --index-url https://download.pytorch.org/whl/cpu \
 && pip install --no-cache-dir unbabel-comet==2.2.7 "numpy<2"
ENV HF_HOME=/root/.cache/huggingface TOKENIZERS_PARALLELISM=false
WORKDIR /w
