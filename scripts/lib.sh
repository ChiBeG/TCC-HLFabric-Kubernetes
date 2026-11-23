# Funções comuns. Use: source "$(dirname "$0")/lib.sh"
set -euo pipefail

RAIZ="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck disable=SC1091
source "$RAIZ/config.env"

log()   { printf '\n\033[1;34m==> %s\033[0m\n' "$*"; }
ok()    { printf '\033[1;32m    ok: %s\033[0m\n' "$*"; }
aviso() { printf '\033[1;33m    aviso: %s\033[0m\n' "$*"; }
erro()  { printf '\033[1;31m    erro: %s\033[0m\n' "$*" >&2; exit 1; }

# esperar <descrição> <timeout_s> <comando...>  -> repete até o comando ter sucesso
esperar() {
  local desc=$1 limite=$2; shift 2
  local fim=$(( $(date +%s) + limite ))
  printf '    aguardando: %s' "$desc"
  until "$@" >/dev/null 2>&1; do
    [ "$(date +%s)" -ge "$fim" ] && { echo; erro "timeout aguardando: $desc"; }
    printf '.'; sleep 3
  done
  echo; ok "$desc"
}

# Registra versão de um componente em artifacts/versoes.txt (reprodutibilidade)
registrar_versao() {
  mkdir -p "$ARTIFACTS"
  local arq="$ARTIFACTS/versoes.txt"
  touch "$arq"
  grep -v "^$1=" "$arq" > "$arq.tmp" || true
  echo "$1=$2" >> "$arq.tmp"
  sort "$arq.tmp" > "$arq"; rm -f "$arq.tmp"
}

exigir_control_plane() {
  [ "$(hostname)" = "control-plane" ] || erro "execute no control-plane"
  [ "$(id -u)" -ne 0 ] || erro "execute como usuário vagrant (sem sudo)"
}

# Estado da rede Fabric fica fora da pasta compartilhada (contém chaves)
ESTADO="$HOME/fabric-estado"
