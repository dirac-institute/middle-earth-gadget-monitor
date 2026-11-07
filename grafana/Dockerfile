FROM docker.io/grafana/grafana:13.2.3

# Datasource, dashboard provider, and dashboards are baked in so a deploy is
# fully described by the repo. grafana.db (users, UI-saved dashboards) lives
# on /local and is backed up to shire.
COPY provisioning/ /etc/grafana/provisioning/
COPY dashboards/       /etc/grafana/dashboards/
COPY dashboards-admin/ /etc/grafana/dashboards-admin/
COPY dashboards-uw/    /etc/grafana/dashboards-uw/
COPY entrypoint.sh /usr/local/bin/gadget-grafana-entrypoint.sh
COPY access.py /usr/local/bin/gadget-access.py

# access.py sets folder permissions and keeps the UW NetID team in sync.
USER root
RUN apk add --no-cache python3
USER 472

ENTRYPOINT ["/usr/local/bin/gadget-grafana-entrypoint.sh"]
