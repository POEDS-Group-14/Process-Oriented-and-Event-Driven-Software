#!/bin/sh
# Install or update process 1 on a Debian server, as systemd services that start on boot.
# Run from the laptop:   processes/p1/deploy/install.sh liam@pinklady.local
# It copies this folder to ~/rejsegaranti-src on the server, then runs setup-server.sh there with sudo.
set -eu
HOST=${1:?usage: deploy/install.sh user@host}
cd "$(dirname "$0")/.."
rsync -a --delete --exclude out/ --exclude .venv --exclude venv.nosync/ --exclude __pycache__/ \
  --exclude .DS_Store --exclude '*.pdf' --exclude '*.png' --exclude '*.svg' \
  ./ "$HOST":rejsegaranti-src/
ssh -t "$HOST" 'sudo sh rejsegaranti-src/deploy/setup-server.sh'
