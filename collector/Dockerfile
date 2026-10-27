FROM docker.io/library/python:3.13.15-slim-trixie

RUN apt-get update && apt-get install -y --no-install-recommends openssh-client && \
    rm -rf /var/lib/apt/lists/*

ARG SVC_UID
ARG SVC_GID
ARG SVC_USER

# ssh refuses to run for a uid it cannot resolve, so the image needs the user.
# (The getent guards only matter for dev-startup.sh, where your own gid may
# already exist in the base image.)
RUN (getent group ${SVC_GID} >/dev/null || groupadd -g ${SVC_GID} ${SVC_USER}) && \
    (getent passwd ${SVC_UID} >/dev/null || \
     useradd -u ${SVC_UID} -g ${SVC_GID} -M -d /tmp -s /usr/sbin/nologin ${SVC_USER})

COPY collector/collector.py /srv/collector/collector.py
COPY agent/agent.py         /srv/agent/agent.py
COPY checks/                /srv/checks/
COPY config/                /srv/config/

CMD ["python3", "-u", "/srv/collector/collector.py"]
