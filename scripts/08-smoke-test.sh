#!/usr/bin/env bash
# Control-plane, usuário vagrant.
# Teste funcional do chaincode com as duas organizações (regras de negócio e permissões).
# Usa um CPF novo a cada execução (série 00000, reservada para testes manuais).
source "$(dirname "$0")/lib.sh"
exigir_control_plane
cd "$ESTADO"

CPF="00000$(printf '%06d' $(( $(date +%s) % 1000000 )))"
VALIDADE=2027-12-31
FALHAS=0

cc() {  # cc <invoke|query> <org> <função> [args...]
  local modo=$1 org=$2 fcn=$3; shift 3
  local args=(); for a in "$@"; do args+=(-a "$a"); done
  kubectl hlf chaincode "$modo" --config="$org.yaml" --user=admin --peer="$org-peer0.default" \
    --chaincode="$CHAINCODE" --channel="$CANAL" --fcn="$fcn" "${args[@]}" 2>&1
}

passo() { printf '\n--- %s\n' "$*"; }
falhou() { printf '\033[1;31m    FALHOU: %s\033[0m\n' "$*"; FALHAS=$((FALHAS+1)); }

# deve_passar <descrição> <texto esperado na saída|""> <cc args...>
deve_passar() {
  local desc=$1 esperado=$2; shift 2; local out
  passo "$desc"
  if out=$(cc "$@") && { [ -z "$esperado" ] || grep -q "$esperado" <<<"$out"; }; then
    echo "$out" | tail -3; ok "$desc"
  else
    echo "$out" | tail -5; falhou "$desc"
  fi
}

# deve_falhar <descrição> <trecho da mensagem de erro> <cc args...>
deve_falhar() {
  local desc=$1 esperado=$2; shift 2; local out
  passo "$desc (deve ser rejeitado)"
  out=$(cc "$@") || true
  if grep -q "$esperado" <<<"$out"; then
    ok "rejeitado com a mensagem esperada"
  else
    echo "$out" | tail -5; falhou "$desc"
  fi
}

log "Smoke test do chaincode (CPF $CPF)"
deve_passar "Org1 emite vínculo"        ""          invoke org1 EmitirVinculo "$CPF" UNIRIO "$VALIDADE"
sleep 3   # espera o bloco (BatchTimeout 2s)
deve_passar "Org2 verifica (ativo)"     '"ativo"'   query  org2 VerificarVinculo "$CPF"
deve_falhar "Org1 emite de novo"        "já possui vínculo ativo"        invoke org1 EmitirVinculo "$CPF" UNIRIO "$VALIDADE"
deve_falhar "Org2 tenta emitir"         "restrita à organização emissora" invoke org2 EmitirVinculo "${CPF%?}9" UFRJ "$VALIDADE"
deve_falhar "Org2 tenta revogar"        "restrita à organização emissora" invoke org2 RevogarVinculo "$CPF" UNIRIO
deve_falhar "Revogar por outra instituição" "não por"    invoke org1 RevogarVinculo "$CPF" UFRJ
deve_falhar "Validade inválida"         "validade inválida"   invoke org1 EmitirVinculo "${CPF%?}8" UNIRIO 31/12/2027
deve_passar "Org1 revoga"               ""          invoke org1 RevogarVinculo "$CPF" UNIRIO
sleep 3
deve_passar "Org2 verifica (revogado)"  '"revogado"' query org2 VerificarVinculo "$CPF"
deve_passar "Histórico (2 versões)"     'revogado'  query  org2 HistoricoVinculo "$CPF"
deve_falhar "CPF inexistente"           "não encontrado" query org2 VerificarVinculo 99999999999

echo
[ "$FALHAS" -eq 0 ] || erro "$FALHAS verificação(ões) falharam"
log "Smoke test OK. Próximo: scripts/09-caliper.sh"
