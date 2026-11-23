#!/usr/bin/env bash
# Control-plane, usuário vagrant.
# Prepara o workspace do Caliper em ~/caliper-vinculo e roda um benchmark de fumaça.
# O Caliper roda no control-plane para não disputar CPU com os componentes medidos (workers).
source "$(dirname "$0")/lib.sh"
exigir_control_plane
CAL="$HOME/caliper-vinculo"

# --- Node.js via nvm (versão fixada no config.env) ---
export NVM_DIR="$HOME/.nvm"
if [ ! -s "$NVM_DIR/nvm.sh" ]; then
  log "Instalando nvm"
  curl -fsSL https://raw.githubusercontent.com/nvm-sh/nvm/v0.40.1/install.sh | bash
fi
# shellcheck disable=SC1091
set +u; source "$NVM_DIR/nvm.sh"; nvm install "$NODE_MAJOR" >/dev/null; nvm use "$NODE_MAJOR" >/dev/null; set -u
ok "node $(node -v)"
command -v python3 >/dev/null && python3 -c 'import matplotlib' 2>/dev/null \
  || sudo NEEDRESTART_MODE=l DEBIAN_FRONTEND=noninteractive apt-get install -y -q python3-matplotlib >/dev/null

# --- Workspace (código versionado no projeto; chaves ficam só no home) ---
log "Workspace $CAL"
mkdir -p "$CAL"/{networks,workload,profiles,identities/org1,identities/org2}
cp "$RAIZ/caliper/networks/network.yaml" "$CAL/networks/"
cp "$RAIZ"/caliper/workload/*.js "$CAL/workload/"
cp "$RAIZ/caliper/benchmarks.py" "$RAIZ/caliper/experimento.py" "$CAL/"
chmod +x "$CAL/experimento.py" "$CAL/benchmarks.py"
cd "$CAL"
[ -f package.json ] || npm init -y >/dev/null
if [ ! -d node_modules/@hyperledger/caliper-cli ]; then
  log "Instalando Caliper $CALIPER_VERSION (alguns minutos)"
  npm install --no-fund --no-audit "@hyperledger/caliper-cli@$CALIPER_VERSION"
fi
if [ ! -d node_modules/@hyperledger/fabric-gateway ]; then
  log "Bind $CALIPER_SUT"
  npx caliper bind --caliper-bind-sut "$CALIPER_SUT"
fi

# --- Identidades do Caliper (tipo client, registradas no 05) e perfis de conexão ---
log "Identidades e perfis"
for org in org1 org2; do
  msp=$([ $org = org1 ] && echo Org1MSP || echo Org2MSP)
  kubectl hlf ca enroll --name="$org-ca" --user=caliper --secret="$CALIPER_PW" --mspid="$msp" \
    --ca-name=ca --output="$ESTADO/caliper-$org.yaml" --ca-url="https://$org-ca.localho.st"
  python3 - "$ESTADO/caliper-$org.yaml" "identities/$org" <<'EOF'
import sys, yaml
d = yaml.safe_load(open(sys.argv[1]))
open(f"{sys.argv[2]}/cert.pem", "w").write(d["cert"]["pem"])
open(f"{sys.argv[2]}/key.pem", "w").write(d["key"]["pem"])
EOF
  chmod 600 "identities/$org/key.pem"
  cp "$ESTADO/$org.yaml" "profiles/$org-ccp.yaml"
  ok "$org: identidade caliper e perfil"
done

# --- Fumaça: 50 emissões (Org1), 50 verificações (Org2), 50 revogações (Org1) ---
log "Benchmark de fumaça"
BENCH=$(python3 benchmarks.py fumaca)
TX=$(mktemp -d)/tx
CALIPER_TXLOG="$TX" npx caliper launch manager --caliper-workspace . \
  --caliper-networkconfig networks/network.yaml --caliper-benchconfig "$BENCH" \
  --caliper-flow-only-test > "$ESTADO/caliper-fumaca.log" 2>&1 \
  || { tail -30 "$ESTADO/caliper-fumaca.log"; erro "Caliper falhou (log em $ESTADO/caliper-fumaca.log)"; }
python3 - "$TX" <<'EOF'
import csv, glob, sys
from collections import Counter
c = Counter()
for arq in glob.glob(sys.argv[1] + ".w*.csv"):
    for r in csv.DictReader(open(arq)):
        c[(r["op"], r["status"])] += 1
for (op, st), n in sorted(c.items()):
    print(f"    {op:10} {st:8} {n}")
falhas = sum(n for (op, st), n in c.items() if st != "success")
sys.exit(1 if falhas or sum(c.values()) != 150 else 0)
EOF
ok "fumaça: 150/150 transações com sucesso"

registrar_versao node "$(node -v)"
registrar_versao caliper "$CALIPER_VERSION ($CALIPER_SUT)"
log "Caliper pronto. Experimentos: cd $CAL && ./experimento.py --help"