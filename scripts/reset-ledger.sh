#!/usr/bin/env bash
# Control-plane, usuário vagrant.
# Apaga o ledger (canal, peers, orderer e volumes) mantendo cluster, CAs e identidades,
# e recria a rede com 06 -> 07 -> 08 -> 09. Útil para começar uma campanha de
# experimentos com ledger vazio (mesmas condições iniciais em todas as campanhas).
#
#   scripts/reset-ledger.sh          pede confirmação
#   scripts/reset-ledger.sh --sim    sem confirmação
source "$(dirname "$0")/lib.sh"
exigir_control_plane

if [ "${1:-}" != "--sim" ]; then
  read -r -p "Isto apaga TODO o ledger do canal $CANAL. Digite APAGAR para continuar: " resp
  [ "$resp" = "APAGAR" ] || erro "cancelado"
fi

log "Removendo canal, chaincode, peers e orderer"
kubectl delete fabricfollowerchannels.hlf.kungfusoftware.es --all --wait=true || true
kubectl delete fabricmainchannels.hlf.kungfusoftware.es --all --wait=true || true
kubectl delete fabricchaincodes.hlf.kungfusoftware.es --all --wait=true || true
kubectl delete fabricpeers.hlf.kungfusoftware.es --all --wait=true || true
kubectl delete fabricorderernodes.hlf.kungfusoftware.es --all --wait=true || true
esperar "pods de peers/orderer/chaincode removidos" 300 bash -c \
  "! kubectl get pods --no-headers 2>/dev/null | grep -qE '^(org[12]-peer0|ord-node1|$CHAINCODE)-'"

# Volumes (o local-path apaga os dados no nó quando o PVC é removido)
PVCS=$(kubectl get pvc -o name | grep -E 'org[12]-peer0|ord-node1' || true)
[ -n "$PVCS" ] && kubectl delete $PVCS --wait=true
ok "ledger removido"

# Estado derivado do ledger antigo
rm -f "$ESTADO"/org1.yaml "$ESTADO"/org2.yaml "$HOME/caliper-vinculo/serie.txt"

log "Recriando a rede"
bash "$RAIZ/scripts/06-fabric-network.sh"
CC_SEQUENCE=1 bash "$RAIZ/scripts/07-chaincode.sh"
bash "$RAIZ/scripts/08-smoke-test.sh"
bash "$RAIZ/scripts/09-caliper.sh"
log "Ledger zerado e rede validada"
