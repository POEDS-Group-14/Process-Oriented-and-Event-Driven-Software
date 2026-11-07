#!/bin/sh
# Runs on the server as root (via deploy/install.sh). Safe to rerun: it updates the code and restarts
# the services, but keeps Camunda's database, the bridge's state and the recorder's data.
set -eu
SRC=$(getent passwd "${SUDO_USER:?run with sudo}" | cut -d: -f6)/rejsegaranti-src
CAMUNDA_URL=https://downloads.camunda.cloud/release/camunda-bpm/run/7.24/camunda-bpm-run-7.24.0.tar.gz

command -v java >/dev/null || { apt-get update -q; apt-get install -y openjdk-21-jre-headless; }
id rejsegaranti >/dev/null 2>&1 || useradd --system --home-dir /opt/rejsegaranti --no-create-home \
  --shell /usr/sbin/nologin rejsegaranti

if [ ! -x /opt/camunda/internal/run.sh ]; then
  mkdir -p /opt/camunda /etc/rejsegaranti
  curl -fsSL "$CAMUNDA_URL" | tar -xz -C /opt/camunda
  # The admin password replaces demo/demo; it is kept in /etc/rejsegaranti/camunda-admin
  [ -f /etc/rejsegaranti/camunda-admin ] || (umask 077; head -c 12 /dev/urandom | base64 | tr -d '/+=' \
    > /etc/rejsegaranti/camunda-admin)
  # admin/<password> instead of demo/demo; no demo invoice process; port 8080 is taken on pinklady
  sed -i -e "s/^\(\s*\)id: demo/\1id: admin/" -e "s/^\(\s*\)password: demo/\1password: $(cat /etc/rejsegaranti/camunda-admin)/" \
    -e "/example:/,/enabled:/ s/enabled: true/enabled: false/" /opt/camunda/configuration/default.yml
  printf '\nserver.port: 8085\n' >> /opt/camunda/configuration/default.yml
  chown -R rejsegaranti:rejsegaranti /opt/camunda
fi

# Process 1 (service + models) and the weather recorder. The recorder's key file goes to /etc, readable
# by the service user only, and is not copied into /opt.
mkdir -p /opt/rejsegaranti
rsync -a --delete --exclude recorder/deploy/recorder.env "$SRC"/ /opt/rejsegaranti/
chown -R root:root /opt/rejsegaranti && chmod -R u=rwX,go=rX /opt/rejsegaranti
[ -f /etc/rejsegaranti-recorder.env ] || install -m 600 -o rejsegaranti -g rejsegaranti \
  "$SRC"/recorder/deploy/recorder.env /etc/rejsegaranti-recorder.env

install -m 644 /opt/rejsegaranti/deploy/*.service /opt/rejsegaranti/recorder/deploy/rejsegaranti-recorder.service \
  /etc/systemd/system/
systemctl daemon-reload
systemctl enable rejsegaranti-camunda rejsegaranti-bridge rejsegaranti-feed rejsegaranti-recorder
systemctl start rejsegaranti-camunda
systemctl restart rejsegaranti-recorder
systemctl restart --no-block rejsegaranti-bridge rejsegaranti-feed
systemctl --no-pager --lines=0 status rejsegaranti-camunda rejsegaranti-bridge rejsegaranti-feed \
  rejsegaranti-recorder || true
