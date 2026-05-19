
ARG OLLAMA_TAG=latest
FROM ollama/ollama:${OLLAMA_TAG}

ARG PYTHON_VERSION=3.12 \
    PYTHON_FREE_THREADING=0 \
    TMP=/tmp/python \
    PROXY

ENV PYTHON_VERSION=${PYTHON_VERSION} \
    PYTHON_FREE_THREADING=${PYTHON_FREE_THREADING} \
    PYTHONFAULTHANDLER=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONIOENCODING=utf-8 \
    PYTHONHASHSEED=random \
    TWINE_NON_INTERACTIVE=1 \
    DEBIAN_FRONTEND=noninteractive \
    PATH=/opt/venv/bin:$PATH \
    UV_PYTHON=/opt/venv/bin/python

COPY install_python.sh ${TMP}/
RUN PROXY=${PROXY} ${TMP}/install_python.sh

# Set PYTHON_GIL=0 for free-threaded builds
RUN if [ "${PYTHON_FREE_THREADING}" = "1" ]; then \
      echo "export PYTHON_GIL=0" >> /etc/bash.bashrc; \
    fi

WORKDIR /app
COPY requirements.txt /app/requirements.txt
RUN uv pip install --no-cache --python /opt/venv/bin/python -r /app/requirements.txt

COPY gateway.py start.sh /app/
RUN chmod +x /app/start.sh

EXPOSE 11535

CMD ["/app/start.sh"]
