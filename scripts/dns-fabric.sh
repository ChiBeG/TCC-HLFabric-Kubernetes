#!/usr/bin/env bash
# Configura a resolução dos nomes *.localho.st do Fabric.
#
#   Peers e orderer  -> Services internos (sem passar pelo Istio)
#   CAs e admin do orderer -> ingress do Istio (IP do MetalLB)
#
# Dentro do cluster: CoreDNS (rewrite pelo NOME do Service, que não muda).
# No control-plane: /etc/hosts com os ClusterIPs atuais (rodar de novo após recriar peers/orderer).
source "$(dirname "$0")/lib.sh"
exigir_control_plane

INTERNOS=( "peer0-org1:org1-peer0" "peer0-org2:org2-peer0" "orderer0-ord:ord-node1" )
EXTERNOS=( org1-ca org2-ca ord-ca admin-orderer0-ord )

log "CoreDNS"
REWRITES=""
for par in "${INTERNOS[@]}"; do
  REWRITES+="        rewrite name exact ${par%%:*}.localho.st ${par##*:}.default.svc.cluster.local answer auto"$'\n'
done
HOSTS=""
for n in "${EXTERNOS[@]}"; do HOSTS+="           $INGRESS_IP $n.localho.st"$'\n'; done

cat > /tmp/Corefile <<EOF
.:53 {
        errors
        health {
           lameduck 5s
        }
        ready
${REWRITES}        hosts {
${HOSTS}           fallthrough
        }
        kubernetes cluster.local in-addr.arpa ip6.arpa {
           pods insecure
           fallthrough in-addr.arpa ip6.arpa
           ttl 30
        }
        prometheus :9153
        forward . /etc/resolv.conf {
           max_concurrent 1000
        }
        cache 30
        loop
        reload
        loadbalance
}
EOF
kubectl -n kube-system create configmap coredns --from-file=Corefile=/tmp/Corefile \
  --dry-run=client -o yaml | kubectl apply -f -
kubectl -n kube-system rollout restart deploy/coredns
kubectl -n kube-system rollout status deploy/coredns --timeout=120s
cp /tmp/Corefile "$ARTIFACTS/Corefile"

log "/etc/hosts do control-plane"
BLOCO=""
for par in "${INTERNOS[@]}"; do
  ip=$(kubectl get svc "${par##*:}" -o jsonpath='{.spec.clusterIP}' 2>/dev/null || true)
  [ -n "$ip" ] && BLOCO+="$ip ${par%%:*}.localho.st # fabric"$'\n'
done
for n in "${EXTERNOS[@]}"; do BLOCO+="$INGRESS_IP $n.localho.st # fabric"$'\n'; done
sudo sed -i '/# fabric$/d' /etc/hosts
printf '%s' "$BLOCO" | sudo tee -a /etc/hosts >/dev/null
grep '# fabric$' /etc/hosts
