#!/bin/sh
# Runs on the server as root (via deploy/install.sh). Safe to rerun: it updates the code and restarts
# the services, but keeps Camunda's database, the bridge's state and the recorder's data.
set -eu
SRC=$(getent passwd "${SUDO_USER:?run with sudo}" | cut -d: -f6)/rejsegaranti-src
CAMUNDA_URL=https://downloads.camunda.cloud/release/camunda-bpm/run/7.24/camunda-bpm-run-7.24.0.tar.gz

command -v java >/dev/null && python3 -c 'import azure.servicebus, azure.identity' 2>/dev/null || {
  # Java for Camunda; the Azure SDK for the recorder's Dataudveksleren (Movia) feed
  apt-get update -q; apt-get install -y openjdk-21-jre-headless python3-azure; }
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

# Authorization checks, so the read-only guest login below really is read-only. The bridge and workers
# call the REST API without logging in, which skips the checks, so they are not affected.
RESTART_CAMUNDA=
if ! grep -q '^camunda.bpm.authorization.enabled' /opt/camunda/configuration/default.yml; then
  printf '\ncamunda.bpm.authorization.enabled: true\n' >> /opt/camunda/configuration/default.yml
  RESTART_CAMUNDA=1
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
if [ -n "$RESTART_CAMUNDA" ]; then systemctl restart rejsegaranti-camunda; else systemctl start rejsegaranti-camunda; fi
systemctl restart rejsegaranti-recorder
systemctl restart --no-block rejsegaranti-bridge rejsegaranti-feed
# Read-only Cockpit login "guest" for people outside the tailnet; password in /etc/rejsegaranti/camunda-guest
REST=http://localhost:8085/engine-rest
until curl -sf $REST/engine >/dev/null; do sleep 5; done
if ! curl -sf $REST/user/guest/profile >/dev/null; then
  [ -f /etc/rejsegaranti/camunda-guest ] || (umask 077; head -c 9 /dev/urandom | base64 | tr -d '/+=' \
    > /etc/rejsegaranti/camunda-guest)
  curl -sf -H 'Content-Type: application/json' -X POST $REST/user/create -d '{"profile": {"id": "guest",
    "firstName": "Guest", "lastName": "(read-only)"}, "credentials": {"password": "'"$(cat /etc/rejsegaranti/camunda-guest)"'"}}'
  # type 1 = grant. Resource types: 0 application, 6 process definition, 8 process instance,
  # 9 deployment, 10 decision definition
  for auth in '0 cockpit "ACCESS"' '6 * "READ","READ_HISTORY"' '8 * "READ"' '9 * "READ"' '10 * "READ","READ_HISTORY"'; do
    set -f; set -- $auth; set +f
    curl -sf -o /dev/null -H 'Content-Type: application/json' -X POST $REST/authorization/create \
      -d '{"type": 1, "userId": "guest", "resourceType": '"$1"', "resourceId": "'"$2"'", "permissions": ['"$3"']}'
  done
fi

# Public HTTPS through Tailscale Funnel: the results page at / and Cockpit at /camunda. Nothing else:
# the REST API and the bridge's POST endpoints stay on the home network and the tailnet.
tailscale funnel --bg http://127.0.0.1:8090/results >/dev/null
tailscale funnel --bg --set-path /camunda http://127.0.0.1:8085/camunda >/dev/null
tailscale funnel status

systemctl --no-pager --lines=0 status rejsegaranti-camunda rejsegaranti-bridge rejsegaranti-feed \
  rejsegaranti-recorder || true
