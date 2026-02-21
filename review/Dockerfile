FROM python:3.12-alpine

RUN apk add --no-cache git curl

COPY review-server.py /app/review-server.py
COPY review-config.yaml /app/review-config.yaml

WORKDIR /app

RUN adduser -D -u 1000 reviewer

USER reviewer

EXPOSE 8080

CMD ["python3", "review-server.py"]
