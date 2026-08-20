FROM apache/airflow:2.10.5-python3.12

COPY --chown=airflow:root pyproject.toml README.md /opt/prediction_markets/
COPY --chown=airflow:root src /opt/prediction_markets/src

# The CPU wheel index, explicitly. PyPI's default torch pulls the bundled
# CUDA runtime -- some four gigabytes of it -- and this image never touches a
# GPU: the embedding and rerank tasks are configured for CPU.
RUN pip install --no-cache-dir \
        --extra-index-url https://download.pytorch.org/whl/cpu \
        "/opt/prediction_markets[ml]"
