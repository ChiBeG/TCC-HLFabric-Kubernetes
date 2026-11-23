#!/usr/bin/env bash
# Executado pelo Vagrant (root) nos workers. Entra no cluster.
set -euo pipefail
source /vagrant/config.env

if [ -f /etc/kubernetes/kubelet.conf ]; then
  echo "==> $(hostname) já faz parte do cluster"; exit 0
fi
[ -f "$ARTIFACTS/join.sh" ] || { echo "erro: $ARTIFACTS/join.sh não existe; suba o control-plane primeiro"; exit 1; }

echo "==> $(hostname) entrando no cluster"
JOIN_CMD=$(cat "$ARTIFACTS/join.sh")
# shellcheck disable=SC2086
$JOIN_CMD --node-name="$(hostname)"
echo "==> $(hostname) ok"
