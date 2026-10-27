FROM docker.io/grafana/grafana:13.2.3

# Datasource, dashboard provider, and dashboards are baked in so a deploy is
# fully described by the repo. grafana.db (users, UI-saved dashboards) lives
# on /local and is backed up to shire.
COPY provisioning/ /etc/grafana/provisioning/
COPY dashboards/   /etc/grafana/dashboards/
COPY entrypoint.sh /usr/local/bin/gadget-grafana-entrypoint.sh

ENTRYPOINT ["/usr/local/bin/gadget-grafana-entrypoint.sh"]
