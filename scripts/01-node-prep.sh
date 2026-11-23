#!/usr/bin/env bash
# Executado pelo Vagrant (root) em TODAS as VMs.
# Prepara o nó: kernel, containerd, kubeadm/kubelet/kubectl e acesso SSH entre nós.
# Uso: 01-node-prep.sh <ip-do-no>
set -euo pipefail
source /vagrant/config.env
NODE_IP=$1
export DEBIAN_FRONTEND=noninteractive

echo "==> [$(hostname)] preparando nó ($NODE_IP)"

# --- Swap desligado (requisito do kubelet) ---
swapoff -a
sed -i '/ swap / s/^/#/' /etc/fstab

# --- Módulos e parâmetros de rede ---
cat > /etc/modules-load.d/k8s.conf <<EOF
overlay
br_netfilter
EOF
modprobe overlay
modprobe br_netfilter
cat > /etc/sysctl.d/k8s.conf <<EOF
net.bridge.bridge-nf-call-iptables  = 1
net.bridge.bridge-nf-call-ip6tables = 1
net.ipv4.ip_forward                 = 1
EOF
sysctl --system >/dev/null

# --- Pacotes base ---
apt-get update -q
apt-get install -y -q apt-transport-https ca-certificates curl gpg jq python3-yaml unzip

# --- containerd ---
apt-get install -y -q containerd
mkdir -p /etc/containerd
containerd config default > /etc/containerd/config.toml
sed -i 's/SystemdCgroup = false/SystemdCgroup = true/' /etc/containerd/config.toml
grep -q 'SystemdCgroup = true' /etc/containerd/config.toml \
  || echo "    aviso: não foi possível ativar SystemdCgroup; confira /etc/containerd/config.toml"
systemctl restart containerd
systemctl enable containerd

# --- Repositório do Kubernetes (UMA linha no .list; quebra de linha corrompe o apt) ---
mkdir -p /etc/apt/keyrings
curl -fsSL "https://pkgs.k8s.io/core:/stable:/${K8S_MINOR}/deb/Release.key" \
  | gpg --dearmor --yes -o /etc/apt/keyrings/kubernetes-apt-keyring.gpg
echo "deb [signed-by=/etc/apt/keyrings/kubernetes-apt-keyring.gpg] https://pkgs.k8s.io/core:/stable:/${K8S_MINOR}/deb/ /" \
  > /etc/apt/sources.list.d/kubernetes.list
apt-get update -q
apt-get install -y -q kubelet kubeadm kubectl
apt-mark hold kubelet kubeadm kubectl

# --- kubelet: IP da rede privada + DNS limpo para os pods ---
# 1) --node-ip: sem isso o kubelet registra o IP da NAT (10.0.2.15), igual em todas as VMs.
# 2) --resolv-conf: os pods herdam o DNS do nó. O NAT do VirtualBox repassa os
#    domínios de busca do host (ex.: Tailscale), que travavam a resolução de nomes.
cat > /etc/kubernetes/resolv-pods.conf <<EOF
nameserver 1.1.1.1
nameserver 8.8.8.8
EOF
echo "KUBELET_EXTRA_ARGS=--node-ip=${NODE_IP} --resolv-conf=/etc/kubernetes/resolv-pods.conf" \
  > /etc/default/kubelet
systemctl daemon-reload
systemctl restart kubelet || true

# --- Nomes dos nós ---
sed -i '/# k8s-nodes/d' /etc/hosts
cat >> /etc/hosts <<EOF
${CP_IP} control-plane # k8s-nodes
${W1_IP} worker1 # k8s-nodes
${W2_IP} worker2 # k8s-nodes
EOF

# --- SSH entre nós (o control-plane importa a imagem do chaincode nos workers) ---
mkdir -p "$ARTIFACTS"
if [ ! -f "$ARTIFACTS/id_cluster" ]; then
  ssh-keygen -t ed25519 -N "" -C "tcc-cluster" -f "$ARTIFACTS/id_cluster" -q
fi
install -d -m 700 -o vagrant -g vagrant /home/vagrant/.ssh
install -m 600 -o vagrant -g vagrant "$ARTIFACTS/id_cluster" /home/vagrant/.ssh/id_cluster
grep -qf "$ARTIFACTS/id_cluster.pub" /home/vagrant/.ssh/authorized_keys 2>/dev/null \
  || cat "$ARTIFACTS/id_cluster.pub" >> /home/vagrant/.ssh/authorized_keys
cat > /home/vagrant/.ssh/config <<EOF
Host control-plane worker1 worker2
  IdentityFile ~/.ssh/id_cluster
  StrictHostKeyChecking no
  UserKnownHostsFile /dev/null
  LogLevel ERROR
EOF
chown vagrant:vagrant /home/vagrant/.ssh/config /home/vagrant/.ssh/authorized_keys
chmod 600 /home/vagrant/.ssh/config

echo "==> [$(hostname)] nó pronto: $(kubeadm version -o short), containerd $(containerd --version | awk '{print $3}')"
