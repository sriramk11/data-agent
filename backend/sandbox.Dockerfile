# The actual security boundary: model-generated code runs in a container
# built from this image, with --network none, a read-only root filesystem,
# dropped capabilities, and a non-root user (all enforced by manager.py's
# `docker run` invocation, not by anything in this Dockerfile alone --
# defense in depth means neither layer trusts the other to be sufficient).
FROM python:3.12-slim

RUN pip install --no-cache-dir \
    pandas==2.2.2 \
    numpy==1.26.4 \
    matplotlib==3.9.0 \
    duckdb==1.0.0

RUN useradd --uid 1000 --create-home --shell /usr/sbin/nologin sandboxuser
WORKDIR /app
COPY app/sandbox/runner_server.py /app/runner_server.py
RUN mkdir -p /work/data /work/output && chown -R sandboxuser:sandboxuser /work /app

USER sandboxuser
# No CMD/ENTRYPOINT here on purpose -- manager.py's `docker run` always
# supplies the exact command (`python3 -u /app/runner_server.py`), so a
# `docker run` with no override can't accidentally launch the runner with
# stdin not wired up.
