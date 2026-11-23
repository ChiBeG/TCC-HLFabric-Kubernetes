#!/usr/bin/env bash
# Control-plane, usuário vagrant.
# HLF Operator + plugin, DNS, Autoridades Certificadoras e registro das identidades.
source "$(dirname "$0")/lib.sh"
exigir_control_plane
mkdir -p "$ESTADO"

# --- Operator (versão fixada) ---
log "HLF Operator $HLF_OPERATOR_CHART"
helm repo add kfs https://kfsoftware.github.io/hlf-helm-charts --force-update >/dev/null
helm repo update >/dev/null
# 1.13.x depende de gcr.io/kubebuilder/kube-rbac-proxy:v0.5.0, imagem que não existe mais.
helm upgrade --install hlf-operator kfs/hlf-operator --version="$HLF_OPERATOR_CHART" --wait --timeout 10m
registrar_versao hlf-operator "$(kubectl get deploy -l app.kubernetes.io/name=hlf-operator \
  -o jsonpath='{.items[0].spec.template.spec.containers[0].image}')"

# --- Plugin kubectl-hlf na MESMA versão do operator ---
log "Plugin kubectl-hlf $HLF_PLUGIN_VERSION"
if ! kubectl hlf --help >/dev/null 2>&1; then
  instalado=0
  for url in \
    "https://github.com/hyperledger-bevel/bevel-operator-fabric/releases/download/v${HLF_PLUGIN_VERSION}/hlf-operator_${HLF_PLUGIN_VERSION}_linux_amd64.zip" \
    "https://github.com/hyperledger-bevel/bevel-operator-fabric/releases/download/v${HLF_PLUGIN_VERSION}/hlf-operator_v${HLF_PLUGIN_VERSION}_linux_amd64.zip"; do
    if curl -fsSL "$url" -o /tmp/hlf.zip 2>/dev/null; then
      rm -rf /tmp/hlf && unzip -q -o /tmp/hlf.zip -d /tmp/hlf
      sudo install "$(find /tmp/hlf -name kubectl-hlf -type f | head -1)" /usr/local/bin/kubectl-hlf
      instalado=1; break
    fi
  done
  if [ "$instalado" = 0 ]; then
    aviso "binário fixado não encontrado; instalando a versão mais recente via Krew"
    aviso "isso gera avisos 'unknown field' e exige 'kubectl apply --validate=warn' (já tratado nos scripts)"
    ( set -x; cd "$(mktemp -d)" &&
      curl -fsSLO https://github.com/kubernetes-sigs/krew/releases/latest/download/krew-linux_amd64.tar.gz &&
      tar zxf krew-linux_amd64.tar.gz && ./krew-linux_amd64 install krew )
    export PATH="$HOME/.krew/bin:$PATH"
    grep -q '.krew/bin' ~/.bashrc || echo 'export PATH="$HOME/.krew/bin:$PATH"' >> ~/.bashrc
    kubectl krew install hlf
  fi
fi
if [ -x /usr/local/bin/kubectl-hlf ]; then
  registrar_versao kubectl-hlf "$HLF_PLUGIN_VERSION (binário fixado)"
else
  registrar_versao kubectl-hlf "krew: $(kubectl krew info hlf 2>/dev/null | awk '/VERSION/{print $2}')"
fi

# --- DNS (CoreDNS + /etc/hosts) ---
bash "$(dirname "$0")/dns-fabric.sh"

# --- CAs ---
log "Autoridades Certificadoras"
for ca in org1-ca org2-ca ord-ca; do
  if kubectl get fabriccas "$ca" >/dev/null 2>&1; then ok "$ca já existe"; continue; fi
  kubectl hlf ca create --image="$CA_IMAGE" --version="$CA_VERSION" \
    --storage-class="$SC_NAME" --capacity=1Gi --name="$ca" \
    --enroll-id="$ENROLL_ID" --enroll-pw="$ENROLL_PW" \
    --hosts="$ca.localho.st" --istio-port=443 2>&1 | grep -v 'unknown field' || true
done
kubectl wait --timeout=300s --for=condition=Running fabriccas.hlf.kungfusoftware.es --all
for ca in org1-ca org2-ca ord-ca; do
  esperar "$ca respondendo via ingress" 180 curl -fsk "https://$ca.localho.st/cainfo"
done

# --- Registro das identidades (register; o enroll é feito depois) ---
log "Registro de identidades"
registrar() {  # ca usuario senha tipo msp
  kubectl hlf ca register --name="$1" --user="$2" --secret="$3" --type="$4" \
    --enroll-id="$ENROLL_ID" --enroll-secret="$ENROLL_PW" --mspid="$5" \
    --ca-url="https://$1.localho.st" 2>&1 | grep -viE 'already registered|is already' || true
  ok "$1: $2 ($4)"
}
registrar org1-ca peer    "$PEER_PW"    peer    Org1MSP
registrar org1-ca admin   "$ADMIN_PW"   admin   Org1MSP
registrar org1-ca caliper "$CALIPER_PW" client  Org1MSP
registrar org2-ca peer    "$PEER_PW"    peer    Org2MSP
registrar org2-ca admin   "$ADMIN_PW"   admin   Org2MSP
registrar org2-ca caliper "$CALIPER_PW" client  Org2MSP   # cliente do Verificador (consultas)
registrar ord-ca  orderer "$ORDERER_PW" orderer OrdererMSP
registrar ord-ca  admin   "$ADMIN_PW"   admin   OrdererMSP

log "Base do Fabric pronta"
kubectl get fabriccas
