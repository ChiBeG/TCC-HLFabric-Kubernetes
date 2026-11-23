#!/usr/bin/env bash
# Control-plane, usuário vagrant.
# Peers e orderer (afinidade de nó + métricas), identidades, canal e perfis de conexão.
source "$(dirname "$0")/lib.sh"
exigir_control_plane
mkdir -p "$ESTADO" "$ARTIFACTS/fabric"
YAMLS="$ARTIFACTS/fabric"
GEN="$RAIZ/scripts/fabric_yaml.py"
apply() { kubectl apply --validate=warn "$@" 2>&1 | grep -v 'unknown field' || true; }

# --- Peers e orderer ---
log "Gerando YAMLs de peers e orderer"
kubectl hlf peer create --statedb=leveldb --image="$PEER_IMAGE" --version="$PEER_VERSION" \
  --storage-class="$SC_NAME" --enroll-id=peer --enroll-pw="$PEER_PW" --mspid=Org1MSP \
  --capacity=5Gi --name=org1-peer0 --ca-name=org1-ca.default \
  --hosts=peer0-org1.localho.st --istio-port=443 --output > "$YAMLS/org1-peer0.yaml"
kubectl hlf peer create --statedb=leveldb --image="$PEER_IMAGE" --version="$PEER_VERSION" \
  --storage-class="$SC_NAME" --enroll-id=peer --enroll-pw="$PEER_PW" --mspid=Org2MSP \
  --capacity=5Gi --name=org2-peer0 --ca-name=org2-ca.default \
  --hosts=peer0-org2.localho.st --istio-port=443 --output > "$YAMLS/org2-peer0.yaml"
kubectl hlf ordnode create --image="$ORDERER_IMAGE" --version="$ORDERER_VERSION" \
  --storage-class="$SC_NAME" --enroll-id=orderer --enroll-pw="$ORDERER_PW" --mspid=OrdererMSP \
  --capacity=2Gi --name=ord-node1 --ca-name=ord-ca.default \
  --hosts=orderer0-ord.localho.st --admin-hosts=admin-orderer0-ord.localho.st \
  --istio-port=443 --output > "$YAMLS/ord-node1.yaml"

# Arquitetura planejada: worker1 = Universidade + orderer, worker2 = Verificador
python3 "$GEN" no "$YAMLS/org1-peer0.yaml" worker1
python3 "$GEN" no "$YAMLS/ord-node1.yaml"  worker1
python3 "$GEN" no "$YAMLS/org2-peer0.yaml" worker2

log "Aplicando peers e orderer"
apply -f "$YAMLS/ord-node1.yaml" -f "$YAMLS/org1-peer0.yaml" -f "$YAMLS/org2-peer0.yaml"
# O operator às vezes marca FAILED e corrige sozinho na tentativa seguinte
kubectl wait --timeout=600s --for=condition=Running fabricpeers.hlf.kungfusoftware.es --all
kubectl wait --timeout=600s --for=condition=Running fabricorderernodes.hlf.kungfusoftware.es --all
kubectl get pods -o wide | grep -E 'peer|ord-node'

# Services novos => ClusterIPs novos no /etc/hosts
bash "$RAIZ/scripts/dns-fabric.sh"

# --- Identidades de admin como Secrets (usadas pelo operator para o canal) ---
log "Identidades de administração"
identidade() {  # nome ca ca_interna msp
  kubectl get fabricidentities "$1" >/dev/null 2>&1 && { ok "$1 já existe"; return; }
  kubectl hlf identity create --name "$1" --namespace default --ca-name "$2" --ca-namespace default \
    --ca "$3" --mspid "$4" --enroll-id admin --enroll-secret "$ADMIN_PW"
}
identidade ord-admin-sign ord-ca  ca    OrdererMSP
identidade ord-admin-tls  ord-ca  tlsca OrdererMSP
identidade org1-admin     org1-ca ca    Org1MSP
identidade org2-admin     org2-ca ca    Org2MSP
kubectl wait --timeout=180s --for=condition=Running fabricidentities.hlf.kungfusoftware.es --all

# --- Canal (declarativo) ---
log "Canal $CANAL"
CANAL="$CANAL" python3 "$GEN" canal "$YAMLS"
apply -f "$YAMLS/canal.yaml"
kubectl wait --timeout=300s --for=condition=Running "fabricmainchannels.hlf.kungfusoftware.es/$CANAL"
apply -f "$YAMLS/canal-seguidores.yaml"
kubectl wait --timeout=300s --for=condition=Running fabricfollowerchannels.hlf.kungfusoftware.es --all

esperar "org1-peer0 commitando blocos" 180 bash -c \
  "kubectl logs deploy/org1-peer0 | grep -q 'Committed block'"
esperar "org2-peer0 commitando blocos" 180 bash -c \
  "kubectl logs deploy/org2-peer0 | grep -q 'Committed block'"

# --- Perfis de conexão para o kubectl hlf (chaincode, testes) e Caliper ---
log "Perfis de conexão"
cd "$ESTADO"
for org in org1 org2; do
  msp=$([ $org = org1 ] && echo Org1MSP || echo Org2MSP)
  kubectl hlf ca enroll --name="$org-ca" --user=admin --secret="$ADMIN_PW" --mspid="$msp" \
    --ca-name=ca --output="$org-admin.yaml" --ca-url="https://$org-ca.localho.st"
  kubectl hlf inspect --output="$org.yaml" -o "$msp" -o OrdererMSP
  kubectl hlf utils adduser --userPath="$org-admin.yaml" --config="$org.yaml" --username=admin --mspid="$msp"
done
kubectl hlf chaincode queryinstalled --config=org1.yaml --user=admin --peer=org1-peer0.default
kubectl hlf chaincode queryinstalled --config=org2.yaml --user=admin --peer=org2-peer0.default

registrar_versao fabric-peer "$PEER_VERSION"
registrar_versao fabric-orderer "$ORDERER_VERSION"
registrar_versao fabric-ca "$CA_VERSION"
log "Rede Fabric pronta (perfis em $ESTADO)"
