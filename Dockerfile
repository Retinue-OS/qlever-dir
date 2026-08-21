FROM docker.io/adfreiburg/qlever:latest

ENV DEBIAN_FRONTEND=noninteractive
ENV PYTHONUNBUFFERED=1

USER root

RUN apt-get update && apt-get install -y --no-install-recommends \
    inotify-tools \
    nginx \
    python3 \
    python3-pip \
    raptor2-utils \
    && rm -rf /var/lib/apt/lists/*

RUN pip3 install --break-system-packages qlever

RUN mkdir -p /index-a /index-b /run/nginx

# Send nginx's logs to the container's stdout/stderr streams so they show up
# in `docker logs` instead of growing unbounded inside the container.
RUN ln -sf /dev/stdout /var/log/nginx/access.log && \
    ln -sf /dev/stderr /var/log/nginx/error.log

COPY nginx.conf /etc/nginx/nginx.conf
COPY build_index.sh /usr/local/bin/build_index.sh
COPY emit_file.sh /usr/local/bin/emit_file.sh
COPY qleverignore_filter.py /usr/local/bin/qleverignore_filter.py
COPY orchestrator.py /usr/local/bin/orchestrator.py
RUN chmod +x /usr/local/bin/build_index.sh /usr/local/bin/emit_file.sh \
    /usr/local/bin/qleverignore_filter.py /usr/local/bin/orchestrator.py

ENV BASE_URI=https://example.org/data/
ENV REBUILD_DELAY=15
ENV INCREMENTAL_DELAY=2
ENV COMPACTION_DELTA_TRIPLES=100000
ENV RECONCILE_INTERVAL=3600

EXPOSE 7001

# Readiness/liveness signal for `docker` / compose `service_healthy`: hits the
# same ASK {} query health_check() in orchestrator.py uses, through nginx on
# 7001. curl isn't installed in this image and python3 already is, so use
# urllib instead of adding a dependency just for this.
HEALTHCHECK --interval=30s --timeout=10s --start-period=60s --retries=3 \
    CMD python3 -c "import sys, urllib.request; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:7001/api?query=ASK+%7B%7D&outputType=json', timeout=5).status == 200 else 1)" || exit 1

ENTRYPOINT ["python3", "/usr/local/bin/orchestrator.py"]
