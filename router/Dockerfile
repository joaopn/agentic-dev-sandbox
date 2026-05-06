FROM alpine:3.20

RUN apk add --no-cache iptables socat

COPY scripts/ /scripts/
RUN chmod +x /scripts/*.sh && mkdir -p /etc/sandbox/rules

ENTRYPOINT ["/scripts/entrypoint.sh"]
