#!/usr/bin/env bash
# Control-plane, usuário vagrant.
# Instala: Helm, StorageClass, Prometheus+Grafana, MetalLB e Istio (só ingress).
source "$(dirname "$0")/lib.sh"
exigir_control_plane

log "Verificando nós"
esperar "3 nós Ready" 600 bash -c '[ "$(kubectl get nodes --no-headers | grep -c " Ready ")" -eq 3 ]'
kubectl get nodes -o wide
kubectl get nodes -o jsonpath='{range .items[*]}{.status.addresses[?(@.type=="InternalIP")].address}{"\n"}{end}' \
  | grep -q '^10\.0\.2\.' && erro "algum nó registrou o IP da NAT (10.0.2.x); revise --node-ip"

# --- Helm ---
log "Helm $HELM_VERSION"
if ! helm version --short 2>/dev/null | grep -q "$HELM_VERSION"; then
  curl -fsSL "https://get.helm.sh/helm-${HELM_VERSION}-linux-amd64.tar.gz" | tar xz -C /tmp
  sudo install /tmp/linux-amd64/helm /usr/local/bin/helm
fi
registrar_versao helm "$(helm version --short)"

# --- StorageClass (kubeadm não traz nenhuma) ---
log "local-path-provisioner"
kubectl apply -f https://raw.githubusercontent.com/rancher/local-path-provisioner/master/deploy/local-path-storage.yaml
kubectl patch storageclass local-path \
  -p '{"metadata":{"annotations":{"storageclass.kubernetes.io/is-default-class":"true"}}}'
esperar "local-path-provisioner" 180 kubectl -n local-path-storage rollout status deploy/local-path-provisioner --timeout=5s
registrar_versao local-path "$(kubectl -n local-path-storage get deploy local-path-provisioner -o jsonpath='{.spec.template.spec.containers[0].image}')"

# --- Prometheus + Grafana ---
log "kube-prometheus-stack"
helm repo add prometheus-community https://prometheus-community.github.io/helm-charts --force-update >/dev/null
helm repo update >/dev/null
helm upgrade --install prometheus prometheus-community/kube-prometheus-stack \
  --namespace monitoring --create-namespace \
  --set grafana.adminPassword="$GRAFANA_ADMIN_PASSWORD" \
  --set grafana.service.type=NodePort \
  --set grafana.service.nodePort="$GRAFANA_NODEPORT" \
  --set prometheus.service.type=NodePort \
  --set prometheus.service.nodePort="$PROMETHEUS_NODEPORT" \
  --set prometheus.prometheusSpec.serviceMonitorSelectorNilUsesHelmValues=false \
  --wait --timeout 15m
registrar_versao kube-prometheus-stack "$(helm list -n monitoring -o json | jq -r '.[0].chart')"

# --- MetalLB: IP real com porta 443 para o ingress ---
log "MetalLB $METALLB_VERSION"
kubectl apply -f "https://raw.githubusercontent.com/metallb/metallb/${METALLB_VERSION}/config/manifests/metallb-native.yaml"
esperar "pods do MetalLB" 300 kubectl -n metallb-system wait --for=condition=Ready pods -l app=metallb --timeout=5s
cat <<EOF | kubectl apply -f -
apiVersion: metallb.io/v1beta1
kind: IPAddressPool
metadata: { name: default-pool, namespace: metallb-system }
spec: { addresses: ["$METALLB_POOL"] }
---
apiVersion: metallb.io/v1beta1
kind: L2Advertisement
metadata: { name: default, namespace: metallb-system }
EOF
registrar_versao metallb "$METALLB_VERSION"

# --- Istio: apenas o ingress gateway, SEM injeção de sidecar ---
log "Istio $ISTIO_VERSION"
if [ ! -x "$HOME/istio-$ISTIO_VERSION/bin/istioctl" ]; then
  (cd "$HOME" && curl -fsSL https://istio.io/downloadIstio | ISTIO_VERSION="$ISTIO_VERSION" sh -)
fi
sudo install "$HOME/istio-$ISTIO_VERSION/bin/istioctl" /usr/local/bin/istioctl
istioctl install -y \
  --set profile=minimal \
  --set 'components.ingressGateways[0].enabled=true' \
  --set 'components.ingressGateways[0].name=istio-ingressgateway' \
  --set 'components.ingressGateways[0].k8s.service.type=LoadBalancer'
kubectl -n istio-system annotate svc istio-ingressgateway --overwrite \
  "metallb.universe.tf/loadBalancerIPs=$INGRESS_IP"
esperar "ingress com IP $INGRESS_IP" 180 bash -c \
  "kubectl -n istio-system get svc istio-ingressgateway -o jsonpath='{.status.loadBalancer.ingress[0].ip}' | grep -qx '$INGRESS_IP'"
# Garantia: o namespace default NUNCA recebe sidecar (conflita com o TLS do Fabric)
kubectl label namespace default istio-injection- 2>/dev/null || true
registrar_versao istio "$ISTIO_VERSION"

log "Addons prontos"
echo "    Grafana:    http://$CP_IP:$GRAFANA_NODEPORT  (admin / senha do config.env)"
echo "    Prometheus: http://$CP_IP:$PROMETHEUS_NODEPORT"
