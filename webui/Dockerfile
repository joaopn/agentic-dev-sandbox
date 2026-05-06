FROM python:3.12-slim

RUN apt-get update \
 && apt-get install -y --no-install-recommends wget \
 && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir \
        aiohttp==3.9.5 \
        asyncssh==2.14.2

# Vendor xterm.js + fit addon at build time so the image carries no
# runtime internet dependency.
ARG XTERM_VERSION=5.3.0
ARG FIT_VERSION=0.8.0
ARG WEB_LINKS_VERSION=0.9.0
ARG WEBGL_VERSION=0.16.0
ARG SEARCH_VERSION=0.13.0
WORKDIR /app/static/vendor
RUN wget -q https://unpkg.com/xterm@${XTERM_VERSION}/lib/xterm.js \
 && wget -q https://unpkg.com/xterm@${XTERM_VERSION}/css/xterm.css \
 && wget -q https://unpkg.com/xterm-addon-fit@${FIT_VERSION}/lib/xterm-addon-fit.js \
 && wget -q https://unpkg.com/xterm-addon-web-links@${WEB_LINKS_VERSION}/lib/xterm-addon-web-links.js \
 && wget -q https://unpkg.com/xterm-addon-webgl@${WEBGL_VERSION}/lib/xterm-addon-webgl.js \
 && wget -q https://unpkg.com/xterm-addon-search@${SEARCH_VERSION}/lib/xterm-addon-search.js

WORKDIR /app
COPY server.py .
COPY static/index.html static/
COPY static/app.js static/
COPY static/style.css static/

RUN useradd -m -u 1000 webui \
 && mkdir -p /app/tls \
 && chown -R webui:webui /app
USER webui

EXPOSE 7777
CMD ["python", "-u", "server.py"]
