FROM python:3.14.6-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    TZ=Asia/Shanghai

WORKDIR /usr/src/app

# 直接按 Pipfile.lock 的锁定版本安装，避免在镜像里装 pipenv（构建更快、更稳）
# 国内构建可加：--build-arg PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple
ARG PIP_INDEX_URL=https://pypi.org/simple
ENV PIP_INDEX_URL=${PIP_INDEX_URL}

COPY Pipfile.lock ./
RUN python -c "import json; d = json.load(open('Pipfile.lock'))['default']; print('\n'.join(k + v.get('version', '') for k, v in d.items()))" > /tmp/requirements.txt && \
    python -m pip install --no-cache-dir --upgrade pip && \
    python -m pip install --no-cache-dir -r /tmp/requirements.txt && \
    rm -f /tmp/requirements.txt

RUN groupadd --gid 2022 python && \
    useradd --gid 2022 --uid 2022 --create-home \
      --home-dir /usr/src/app python

COPY --chown=python:python lib lib
COPY --chown=python:python models models
COPY --chown=python:python templates templates
COPY --chown=python:python torrentino.py .

RUN mkdir -p config logs && chown -R python:python config logs

USER python

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
  CMD ["python", "-c", "assert b'torrentino.py' in open('/proc/1/cmdline', 'rb').read()"]

ENTRYPOINT ["python", "./torrentino.py"]
