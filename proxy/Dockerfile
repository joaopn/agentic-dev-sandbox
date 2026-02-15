FROM alpine:3.20

RUN apk add --no-cache squid

COPY squid.conf /etc/squid/squid.conf

EXPOSE 3128 3129

# Squid needs a cache directory even if caching is disabled
RUN mkdir -p /var/spool/squid /var/log/squid && \
    chown -R squid:squid /var/spool/squid /var/log/squid /etc/squid

CMD ["squid", "-N", "-f", "/etc/squid/squid.conf"]
