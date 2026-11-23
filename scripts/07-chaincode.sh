#!/usr/bin/env bash
# Control-plane, usuário vagrant.
# Compila o chaincode, gera a imagem, importa nos workers e implanta como serviço externo (CCaaS).
#
# Atualizar o chaincode: altere CC_TAG, CC_VERSION e incremente CC_SEQUENCE no config.env
# (ou exporte as variáveis) e rode este script de novo.
source "$(dirname "$0")/lib.sh"
exigir_control_plane
CC_SRC="$RAIZ/chaincode/vinculo"
BUILD="$ESTADO/chaincode"

# --- Go fixado (o do apt é antigo demais para as dependências) ---
if ! /usr/local/go/bin/go version 2>/dev/null | grep -q "go$GO_VERSION"; then
  log "Instalando Go $GO_VERSION"
  curl -fsSL "https://go.dev/dl/go${GO_VERSION}.linux-amd64.tar.gz" -o /tmp/go.tgz
  sudo rm -rf /usr/local/go && sudo tar -C /usr/local -xzf /tmp/go.tgz
fi
export PATH=/usr/local/go/bin:$PATH
grep -q '/usr/local/go/bin' ~/.bashrc || echo 'export PATH=/usr/local/go/bin:$PATH' >> ~/.bashrc
command -v buildah >/dev/null || sudo NEEDRESTART_MODE=l DEBIAN_FRONTEND=noninteractive apt-get install -y -q buildah

# --- Compilação (binário estático) ---
log "Compilando chaincode"
rm -rf "$BUILD" && mkdir -p "$BUILD" && cp -r "$CC_SRC"/. "$BUILD"/
( cd "$BUILD" && go mod tidy && CGO_ENABLED=0 GOOS=linux go build -o vinculo . && go vet . )
cp "$BUILD/go.mod" "$BUILD/go.sum" "$ARTIFACTS/" 2>/dev/null || true   # dependências exatas usadas

# --- Imagem (tag fixa: com tag != latest o K8s usa a imagem local, sem registry) ---
log "Imagem $CC_IMAGE:$CC_TAG"
( cd "$BUILD" && sudo buildah bud -q -t "$CC_IMAGE:$CC_TAG" . )
sudo rm -f "$ARTIFACTS/$CC_IMAGE.tar"
sudo buildah push "$CC_IMAGE:$CC_TAG" "docker-archive:$ARTIFACTS/$CC_IMAGE.tar:$CC_IMAGE:$CC_TAG"
for w in worker1 worker2; do
  ssh "$w" "sudo ctr -n k8s.io images import $ARTIFACTS/$CC_IMAGE.tar >/dev/null && sudo crictl images 2>/dev/null | grep $CC_IMAGE" \
    || erro "falha ao importar a imagem em $w"
  ok "imagem importada em $w"
done

# --- Pacote CCaaS (só diz ao peer onde o chaincode escuta) ---
log "Pacote e instalação nos peers"
PKG="$ESTADO/cc-pkg" && rm -rf "$PKG" && mkdir -p "$PKG"
echo "{\"address\":\"$CHAINCODE.default:7052\",\"dial_timeout\":\"10s\",\"tls_required\":false}" > "$PKG/connection.json"
echo "{\"type\":\"ccaas\",\"label\":\"$CHAINCODE\"}" > "$PKG/metadata.json"
( cd "$PKG" && tar cfz code.tar.gz connection.json && tar cfz "$CHAINCODE.tgz" metadata.json code.tar.gz )
PACKAGE_ID=$(kubectl hlf chaincode calculatepackageid --path="$PKG/$CHAINCODE.tgz" --language=node --label="$CHAINCODE")
echo "    PACKAGE_ID=$PACKAGE_ID"

cd "$ESTADO"
for org in org1 org2; do
  if kubectl hlf chaincode queryinstalled --config=$org.yaml --user=admin --peer=$org-peer0.default 2>/dev/null \
       | grep -q "$PACKAGE_ID"; then
    ok "já instalado em $org-peer0"
  else
    kubectl hlf chaincode install --path="$PKG/$CHAINCODE.tgz" --config=$org.yaml \
      --language=golang --label="$CHAINCODE" --user=admin --peer=$org-peer0.default
  fi
done

# --- Pod do chaincode ---
log "Serviço do chaincode"
kubectl hlf externalchaincode sync --image="$CC_IMAGE:$CC_TAG" --name="$CHAINCODE" \
  --namespace=default --package-id="$PACKAGE_ID" --tls-required=false --replicas=1 2>&1 \
  | grep -v 'unknown field' || true
esperar "pod do chaincode pronto" 180 kubectl rollout status "deploy/$CHAINCODE" --timeout=5s

# --- Aprovação pelas duas organizações e commit ---
log "Aprovação (sequence $CC_SEQUENCE) e commit"
for org in org1 org2; do
  kubectl hlf chaincode approveformyorg --config=$org.yaml --user=admin --peer=$org-peer0.default \
    --package-id="$PACKAGE_ID" --version="$CC_VERSION" --sequence="$CC_SEQUENCE" \
    --name="$CHAINCODE" --policy="$CC_POLICY" --channel="$CANAL"
done
kubectl hlf chaincode commit --config=org1.yaml --user=admin --mspid=Org1MSP \
  --version="$CC_VERSION" --sequence="$CC_SEQUENCE" --name="$CHAINCODE" \
  --policy="$CC_POLICY" --channel="$CANAL"

registrar_versao chaincode "$CC_IMAGE:$CC_TAG version=$CC_VERSION sequence=$CC_SEQUENCE"
registrar_versao go "$(go version | awk '{print $3}')"
log "Chaincode implantado. Rode scripts/08-smoke-test.sh"