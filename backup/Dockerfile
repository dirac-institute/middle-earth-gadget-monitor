FROM docker.io/victoriametrics/vmbackup:v1.154.0 AS vmbackup
FROM docker.io/victoriametrics/vmrestore:v1.154.0 AS vmrestore

FROM docker.io/library/alpine:3.22.6

RUN apk add --no-cache bash coreutils curl sqlite util-linux

COPY --from=vmbackup  /vmbackup-prod  /usr/local/bin/vmbackup
COPY --from=vmrestore /vmrestore-prod /usr/local/bin/vmrestore
COPY backup-loop.sh backup-once.sh restore.sh /usr/local/bin/

CMD ["/usr/local/bin/backup-loop.sh"]
