# xCOMET-lite (myyycroft/XCOMET-lite, 278M, mDeBERTa-v3) на CPU: оценка перевода без эталона + места ошибок.
# Эксперимент «детектор для гибридного перевода», отдельно от основной системы.
# Сборка (из C:\book_translate): docker.exe build -t bt-xcl:1 experiments\qe
FROM python:3.11-slim
# transformers 4.57: DebertaV2Encoder ждёт от слоя кортеж — под это написан патч слоя в deberta_encoder.py (в 4.36 не работает).
# Версии сверены по исходникам: unbabel-comet 2.2.7 (transformers ^4.17, huggingface-hub <1.0, protobuf ^4).
RUN pip install --no-cache-dir torch==2.4.1 --index-url https://download.pytorch.org/whl/cpu \
 && pip install --no-cache-dir unbabel-comet==2.2.7 transformers==4.57.6 "numpy<2" "huggingface_hub<1.0" \
        "protobuf>=4.24,<5" sentencepiece
# Обёртка модели из репозитория авторов (лицензии в репозитории нет — качаем при сборке, в git не кладём), закреплена по коммиту.
ADD https://raw.githubusercontent.com/NL2G/xCOMET-lite/e21e291b2a09a5a854c55b0c01c53ab580692beb/xcomet/deberta_encoder.py /opt/xcl/deberta_encoder.py
ENV PYTHONPATH=/opt/xcl HF_HOME=/root/.cache/huggingface TOKENIZERS_PARALLELISM=false PYTHONUNBUFFERED=1
WORKDIR /w
