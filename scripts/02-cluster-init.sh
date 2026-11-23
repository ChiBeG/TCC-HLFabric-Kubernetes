#!/usr/bin/env bash
# Executado pelo Vagrant (root) no control-plane.
# Inicializa o cluster, instala o Flannel na interface correta e publica o comando de join.
set -euo pipefail
source /vagrant/config.env

if [ -f /etc/kubernetes/admin.conf ]; then
  echo "==> cluster já inicializado, pulando kubeadm init"
else
  echo "==> kubeadm init"
  kubeadm init \
    --pod-network-cidr="$POD_CIDR" \
    --apiserver-advertise-address="$CP_IP" \
    --node-name=control-plane
fi

# kubeconfig para o usuário vagrant
install -d -o vagrant -g vagrant /home/vagrant/.kube
install -m 600 -o vagrant -g vagrant /etc/kubernetes/admin.conf /home/vagrant/.kube/config
export KUBECONFIG=/etc/kubernetes/admin.conf

# --- Flannel forçado na interface privada ---
mkdir -p "$ARTIFACTS"
curl -fsSL https://github.com/flannel-io/flannel/releases/latest/download/kube-flannel.yml \
  -o "$ARTIFACTS/kube-flannel.yml"
python3 - "$ARTIFACTS/kube-flannel.yml" "$NODE_IFACE" <<'EOF'
import sys
arq, iface = sys.argv[1], sys.argv[2]
linhas, saida = open(arq).read().splitlines(), []
for l in linhas:
    if "--iface=" in l:
        continue                      # remove ocorrências antigas
    saida.append(l)
    if l.strip() == "- --kube-subnet-mgr":
        recuo = l[: len(l) - len(l.lstrip())]
        saida.append(f"{recuo}- --iface={iface}")
open(arq, "w").write("\n".join(saida) + "\n")
EOF
grep -c -- "--iface=$NODE_IFACE" "$ARTIFACTS/kube-flannel.yml" | grep -qx 1 \
  || { echo "erro: --iface não inserido no manifesto do Flannel"; exit 1; }
kubectl apply -f "$ARTIFACTS/kube-flannel.yml"

# --- Comando de join para os workers (token válido por 24h) ---
kubeadm token create --print-join-command > "$ARTIFACTS/join.sh"
chmod 600 "$ARTIFACTS/join.sh"

grep -oE 'image: .*flannel[^ ]*' "$ARTIFACTS/kube-flannel.yml" | head -1 \
  | sed 's/image: /flannel=/' >> "$ARTIFACTS/versoes.txt" || true
echo "kubernetes=$(kubectl version -o json | jq -r .serverVersion.gitVersion)" >> "$ARTIFACTS/versoes.txt"
echo "==> control-plane pronto"
