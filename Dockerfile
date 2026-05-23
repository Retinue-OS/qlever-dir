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

COPY nginx.conf /etc/nginx/nginx.conf
COPY build_index.sh /usr/local/bin/build_index.sh
COPY orchestrator.py /usr/local/bin/orchestrator.py
RUN chmod +x /usr/local/bin/build_index.sh /usr/local/bin/orchestrator.py

ENV BASE_URI=https://example.org/data/
ENV REBUILD_DELAY=15

EXPOSE 7001

ENTRYPOINT ["python3", "/usr/local/bin/orchestrator.py"]
