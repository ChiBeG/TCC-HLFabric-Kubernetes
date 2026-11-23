#!/usr/bin/env python3
"""
Experimentos do TCC (executar no control-plane, dentro de ~/caliper-vinculo).

RQ1, desempenho (taxas crescentes, sem falhas):
    ./experimento.py desempenho [--taxas 10,25,50,100,150] [--duracao 30] [--workers 2]

RQ2/RQ3, resiliência (carga contínua + falha injetada):
    ./experimento.py falha <alvo> --tipo <graciosa|abrupta|particao> [--repeticoes 3]

    alvos de pod   (graciosa, abrupta): peer-org1, peer-org2, orderer, chaincode
    alvos de nó    (particao):          worker1, worker2

Cada execução cria uma pasta em /vagrant/artifacts/resultados com:
    benchmark.yaml, caliper.log, report.html   entrada e saída do Caliper
    transacoes.csv                             uma linha por transação (CALIPER_TXLOG)
    linha_do_tempo.csv                         sucessos, falhas e latência por segundo
    eventos_k8s.csv                            mudanças de estado de nós e pods (polling 1 s)
    prom_<métrica>.csv                         séries do Prometheus no intervalo do teste
    resumo.json, grafico.png                   métricas finais (gráfico se houver matplotlib)

Definições usadas no texto:
    RTO de infraestrutura: falha -> substituto Ready no Kubernetes (pods)
                           ou falha -> nó e pods Ready de novo (partição)
    RTO de serviço:        falha -> primeira transação bem-sucedida criada depois da
                           última transação que falhou (por fluxo: emitir, verificar)
    Convergência:          falha -> peers de volta à mesma altura de ledger (Prometheus)
"""
import argparse
import csv
import json
import math
import os
import re
import shutil
import statistics
import subprocess
import threading
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

import benchmarks

AQUI = Path(__file__).resolve().parent


# ======================================================================
# Configuração (config.env do projeto, compartilhado em /vagrant)
# ======================================================================

def ler_config(caminho=os.environ.get("TCC_CONFIG", "/vagrant/config.env")):
    cfg = {}
    for linha in Path(caminho).read_text().splitlines():
        linha = linha.split("#", 1)[0].strip()
        if "=" in linha:
            k, v = linha.split("=", 1)
            cfg[k.strip()] = v.strip().strip('"').strip("'")
    return cfg


CFG = ler_config()
CANAL = CFG.get("CANAL", "canal-vinculo")
IFACE = CFG.get("NODE_IFACE", "enp0s8")
PROMETHEUS = CFG.get("PROMETHEUS_URL", "http://192.168.56.10:30900")
SAIDA = Path(CFG.get("ARTIFACTS", "/vagrant/artifacts")) / "resultados"

ALVOS_POD = {"peer-org1": "org1-peer0", "peer-org2": "org2-peer0",
             "orderer": "ord-node1", "chaincode": CFG.get("CHAINCODE", "vinculo")}
ALVOS_NO = ["worker1", "worker2"]

CONSULTAS_PROM = {
    "altura_ledger": f'max by (service) (ledger_blockchain_height{{channel="{CANAL}"}})',
    "commits_validos_tps": ('sum by (service) (rate(ledger_transaction_count'
                            f'{{channel="{CANAL}",validation_code="VALID"}}[10s]))'),
    "cpu_pods": ('sum by (pod) (rate(container_cpu_usage_seconds_total'
                 '{namespace="default",container!="",container!="POD"}[30s]))'),
    "memoria_pods_bytes": ('sum by (pod) (container_memory_working_set_bytes'
                           '{namespace="default",container!="",container!="POD"})'),
    "cpu_nos": '1 - avg by (instance) (rate(node_cpu_seconds_total{mode="idle"}[30s]))',
}


# ======================================================================
# Utilitários
# ======================================================================

def agora():
    return time.time()


def epoch_iso(texto):
    return datetime.strptime(texto, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp()


def kubectl_json(*args):
    saida = subprocess.run(["kubectl", *args, "-o", "json"], check=True,
                           capture_output=True, text=True).stdout
    return json.loads(saida)


def pronto(obj):
    """(ready?, lastTransitionTime) de um pod ou nó."""
    for c in obj.get("status", {}).get("conditions", []):
        if c["type"] == "Ready":
            return c["status"] == "True", epoch_iso(c["lastTransitionTime"])
    return False, None


def seletor_deploy(nome):
    return kubectl_json("get", "deploy", nome)["spec"]["selector"]["matchLabels"]


def casa(labels, seletor):
    return all(labels.get(k) == v for k, v in seletor.items())


def esperar(condicao, limite=600, intervalo=1.0, desc="condição"):
    fim = agora() + limite
    while agora() < fim:
        r = condicao()
        if r:
            return r
        time.sleep(intervalo)
    raise TimeoutError(f"timeout aguardando {desc}")


def ambiente_saudavel():
    """Todos os nós Ready e todos os componentes Fabric com réplica pronta."""
    for no in kubectl_json("get", "nodes")["items"]:
        if not pronto(no)[0]:
            return False
    for nome in ALVOS_POD.values():
        st = kubectl_json("get", "deploy", nome)["status"]
        if st.get("readyReplicas", 0) < 1 or st.get("unavailableReplicas", 0):
            return False
    return True


def salvar_csv(arquivo, linhas, campos=None):
    if not linhas:
        return
    campos = campos or list(linhas[0].keys())
    with open(arquivo, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=campos)
        w.writeheader()
        w.writerows(linhas)


def percentil(valores, p):
    if not valores:
        return None
    v = sorted(valores)
    return v[min(len(v) - 1, int(round(p / 100 * (len(v) - 1))))]


def r3(x):
    return None if x is None else round(x, 3)


# ======================================================================
# Monitor do Kubernetes (thread): registra mudanças de estado de nós e pods
# ======================================================================

class MonitorK8s(threading.Thread):
    def __init__(self):
        super().__init__(daemon=True)
        self.parar = threading.Event()
        self.estado = {}      # (tipo, nome) -> ready
        self.eventos = []     # dicts
        self.pods = {}        # nome -> último objeto visto

    def run(self):
        while not self.parar.is_set():
            try:
                itens = kubectl_json("get", "nodes,pods", "-n", "default")["items"]
            except Exception:
                time.sleep(1)
                continue
            t = agora()
            vistos = set()
            for obj in itens:
                tipo = "no" if obj["kind"] == "Node" else "pod"
                nome = obj["metadata"]["name"]
                vistos.add((tipo, nome))
                ready, _ = pronto(obj)
                if tipo == "pod":
                    self.pods[nome] = obj
                if self.estado.get((tipo, nome)) != ready:
                    self.estado[(tipo, nome)] = ready
                    self.eventos.append({
                        "t": t, "tipo": tipo, "nome": nome, "ready": ready,
                        "no": obj.get("spec", {}).get("nodeName", nome if tipo == "no" else ""),
                        "fase": obj.get("status", {}).get("phase", ""),
                    })
            for chave in list(self.estado):
                if chave not in vistos and self.estado[chave] is not None:
                    self.estado[chave] = None
                    self.eventos.append({"t": t, "tipo": chave[0], "nome": chave[1],
                                         "ready": None, "no": "", "fase": "removido"})
            time.sleep(1)


# ======================================================================
# Caliper
# ======================================================================

def achar_npx():
    """npx do PATH ou, se o shell não carregou o nvm, o da versão instalada pelo 09."""
    npx = shutil.which("npx")
    if npx:
        return npx
    candidatos = sorted(Path.home().glob(".nvm/versions/node/*/bin/npx"))
    if not candidatos:
        raise SystemExit("npx não encontrado: rode 'source ~/.bashrc' ou scripts/09-caliper.sh")
    return str(candidatos[-1])


def iniciar_caliper(pasta, bench):
    npx = achar_npx()
    env = dict(os.environ, CALIPER_TXLOG=str(pasta / "tx"))
    env["PATH"] = str(Path(npx).parent) + os.pathsep + env.get("PATH", "")
    cmd = [npx, "caliper", "launch", "manager",
           "--caliper-workspace", str(AQUI),
           "--caliper-networkconfig", "networks/network.yaml",
           "--caliper-benchconfig", str(bench),
           "--caliper-flow-only-test"]
    log = open(pasta / "caliper.log", "w")
    return subprocess.Popen(cmd, cwd=AQUI, env=env, stdout=log, stderr=subprocess.STDOUT)


def esperar_round(pasta, label, proc):
    padrao = re.compile(r"Started round \d+ \(" + re.escape(label) + r"\)")

    def iniciou():
        if proc.poll() is not None:
            raise RuntimeError("Caliper terminou antes do round '%s' (veja caliper.log)" % label)
        return padrao.search((pasta / "caliper.log").read_text(errors="ignore"))
    esperar(iniciou, limite=1800, intervalo=0.5, desc=f"round {label}")


def finalizar_caliper(pasta, proc):
    proc.wait()
    if (AQUI / "report.html").exists():
        shutil.copy(AQUI / "report.html", pasta / "report.html")
    if proc.returncode != 0:
        print(f"[aviso] Caliper terminou com código {proc.returncode} (veja caliper.log)")


def ler_transacoes(pasta):
    txs = []
    for arq in sorted(pasta.glob("tx.w*.csv")):
        worker = arq.name.split(".w")[1].split(".")[0]
        with open(arq) as f:
            for r in csv.DictReader(f):
                txs.append({
                    "round": int(r["round"]), "worker": int(worker), "op": r["op"], "org": r["org"],
                    "cpf": r["cpf"], "ok": r["status"] == "success",
                    "criacao": int(r["t_criacao_ms"]) / 1000, "final": int(r["t_final_ms"]) / 1000,
                })
        arq.unlink()
    txs.sort(key=lambda x: x["criacao"])
    for t in txs:
        t["latencia"] = t["final"] - t["criacao"]
    return txs


# ======================================================================
# Prometheus
# ======================================================================

def prom_range(consulta, inicio, fim, passo="1s"):
    q = urllib.parse.urlencode({"query": consulta, "start": inicio, "end": fim, "step": passo})
    with urllib.request.urlopen(f"{PROMETHEUS}/api/v1/query_range?{q}", timeout=30) as r:
        dados = json.load(r)
    series = {}
    for s in dados["data"]["result"]:
        nome = ",".join(f"{k}={v}" for k, v in s["metric"].items()) or "valor"
        series[nome] = [(float(t), float(v)) for t, v in s["values"]]
    return series


def coletar_prometheus(pasta, inicio, fim, t_ref):
    resultado = {}
    time.sleep(15)  # deixa o último scrape chegar
    for nome, consulta in CONSULTAS_PROM.items():
        try:
            series = prom_range(consulta, inicio, fim)
        except Exception as e:
            print(f"[aviso] Prometheus ({nome}): {e}")
            continue
        linhas = [{"t": t, "t_rel_s": round(t - t_ref, 1), "serie": s, "valor": v}
                  for s, pontos in series.items() for t, v in pontos]
        salvar_csv(pasta / f"prom_{nome}.csv", linhas)
        resultado[nome] = series
    return resultado


def convergencia_ledger(series, t_falha):
    """Divergência máxima de altura entre os peers após a falha e o tempo até voltar a <= 1 bloco."""
    peers = {k: dict(v) for k, v in series.items() if "peer" in k}
    if len(peers) < 2:
        return None, None
    tempos = sorted(set.intersection(*[set(p) for p in peers.values()]))
    div = [(t, max(p[t] for p in peers.values()) - min(p[t] for p in peers.values()))
           for t in tempos if t >= t_falha]
    if not div:
        return None, None
    t_pior, pior = max(div, key=lambda x: x[1])
    if pior <= 1:
        return int(pior), 0.0
    volta = next((t for t, d in div if t > t_pior and d <= 1), None)
    return int(pior), (r3(volta - t_falha) if volta else None)


# ======================================================================
# Análise
# ======================================================================

def estatisticas(txs):
    ok = [t for t in txs if t["ok"]]
    lat = [t["latencia"] for t in ok]
    if not txs:
        return {}
    dur_envio = max(t["criacao"] for t in txs) - min(t["criacao"] for t in txs)
    dur_total = max(t["final"] for t in ok) - min(t["criacao"] for t in txs) if ok else 0
    return {
        "sucesso": len(ok), "falhas": len(txs) - len(ok),
        "taxa_envio_tps": r3(len(txs) / dur_envio) if dur_envio > 0 else None,
        "throughput_tps": r3(len(ok) / dur_total) if dur_total > 0 else None,
        "lat_media_s": r3(statistics.mean(lat)) if lat else None,
        "lat_p50_s": r3(percentil(lat, 50)), "lat_p95_s": r3(percentil(lat, 95)),
        "lat_p99_s": r3(percentil(lat, 99)), "lat_max_s": r3(max(lat)) if lat else None,
    }


def linha_do_tempo(txs, t_ref, ops):
    """Agrupa por segundo (pelo instante de conclusão) relativo a t_ref."""
    if not txs:
        return []
    ini = math.floor(min(t["final"] for t in txs) - t_ref)
    fim = math.floor(max(t["final"] for t in txs) - t_ref)
    linhas = {s: {"t_rel_s": s} for s in range(ini, fim + 1)}
    for s in linhas.values():
        for op in ops:
            s.update({f"{op}_ok": 0, f"{op}_falha": 0, f"{op}_lat_media_s": None})
    acum = {}
    for t in txs:
        s = math.floor(t["final"] - t_ref)
        lin = linhas[s]
        if t["ok"]:
            lin[f"{t['op']}_ok"] += 1
            acum.setdefault((s, t["op"]), []).append(t["latencia"])
        else:
            lin[f"{t['op']}_falha"] += 1
    for (s, op), lat in acum.items():
        linhas[s][f"{op}_lat_media_s"] = r3(statistics.mean(lat))
    return [linhas[s] for s in sorted(linhas)]


def rto_servico(txs, t_falha, janela_normal=10):
    """Métricas de serviço de um fluxo (lista de transações do round de carga)."""
    antes = [t for t in txs if t_falha - 60 <= t["criacao"] < t_falha and t["ok"]]
    base_p95 = percentil([t["latencia"] for t in antes], 95)
    falhas = [t for t in txs if not t["ok"] and t["final"] >= t_falha]
    res = {"base_lat_p95_s": r3(base_p95), "falhas_apos_falha": len(falhas)}

    if falhas:
        ultima = max(falhas, key=lambda t: t["criacao"])
        prox_ok = [t["final"] for t in txs if t["ok"] and t["criacao"] > ultima["criacao"]]
        res["primeira_falha_s"] = r3(min(t["final"] for t in falhas) - t_falha)
        res["ultima_falha_s"] = r3(ultima["final"] - t_falha)
        res["rto_servico_s"] = r3(min(prox_ok) - t_falha) if prox_ok else None
    else:
        res.update(primeira_falha_s=None, ultima_falha_s=None, rto_servico_s=0.0)

    # Degradação: a partir de quando a latência fica normal por `janela_normal` s seguidos
    pos = [t for t in txs if t["ok"] and t["final"] >= t_falha]
    res["lat_max_apos_falha_s"] = r3(max((t["latencia"] for t in pos), default=0))
    if base_p95 is not None and pos:
        limite = max(2 * base_p95, base_p95 + 1.0)
        por_seg = {}
        for t in pos:
            por_seg.setdefault(int(t["final"] - t_falha), []).append(t["latencia"])
        normal_desde, seguidos = None, 0
        for s in range(0, max(por_seg) + 1):
            lat = por_seg.get(s)
            if lat and statistics.mean(lat) <= limite:
                seguidos += 1
                if seguidos == 1:
                    normal_desde = s
                if seguidos >= janela_normal:
                    break
            else:
                seguidos, normal_desde = 0, None
        res["latencia_normal_apos_s"] = normal_desde if seguidos >= janela_normal else None
    return res


def rto_infra_pod(monitor, alvo_pod, seletor, t_falha):
    """Primeiro pod do deployment que ficou Ready após a falha: um pod novo (remoção) ou
    o mesmo pod depois de ter ficado NotReady (container reiniciado pelo kubelet)."""
    caiu = False
    for ev in monitor.eventos:
        if ev["t"] < t_falha or ev["tipo"] != "pod":
            continue
        if ev["nome"] == alvo_pod and ev["ready"] is False:
            caiu = True
            continue
        mesmo_pod_voltou = ev["nome"] == alvo_pod and caiu
        if (ev["ready"] and (ev["nome"] != alvo_pod or mesmo_pod_voltou)
                and casa(monitor.pods.get(ev["nome"], {}).get("metadata", {}).get("labels", {}), seletor)):
            obj = monitor.pods[ev["nome"]]
            _, t_ready = pronto(obj)
            t = t_ready if t_ready and t_ready >= t_falha - 1 else ev["t"]
            return ev["nome"], r3(t - t_falha), r3(ev["t"] - t_falha)
    return None, None, None


def rto_infra_no(monitor, no, t_falha):
    """Partição: quando o nó e os pods nele voltaram a Ready (se o K8s chegou a notar a falha)."""
    caiu = [ev for ev in monitor.eventos if ev["t"] >= t_falha and ev["ready"] is False
            and (ev["nome"] == no or ev["no"] == no)]
    if not caiu:
        return None, "o Kubernetes não chegou a marcar o nó ou os pods como NotReady"
    nomes = {ev["nome"] for ev in caiu}
    voltas = [ev["t"] for ev in monitor.eventos
              if ev["t"] >= t_falha and ev["ready"] and ev["nome"] in nomes]
    return (r3(max(voltas) - t_falha) if voltas else None), f"NotReady observado em: {sorted(nomes)}"


def contar_lockups():
    """Nº de 'soft lockup' no dmesg de cada worker (VM sem CPU do host). None se o ssh falhar."""
    out = {}
    for no in ALVOS_NO:
        try:
            r = subprocess.run(["ssh", no, "sudo dmesg | grep -c 'soft lockup' || true"],
                               capture_output=True, text=True, timeout=20)
            out[no] = int(r.stdout.strip() or 0)
        except Exception:
            out[no] = None
    return out


def nos_inesperados(monitor, t0, alvo_no=None):
    return sorted({ev["nome"] for ev in monitor.eventos
                   if ev["tipo"] == "no" and ev["ready"] is False and ev["t"] >= t0 and ev["nome"] != alvo_no})


# ======================================================================
# Injeção de falhas
# ======================================================================

def derrubar_pod(nome_deploy, tipo):
    seletor = seletor_deploy(nome_deploy)
    sel = ",".join(f"{k}={v}" for k, v in seletor.items())
    pods = [p for p in kubectl_json("get", "pod", "-l", sel)["items"] if pronto(p)[0]]
    if not pods:
        raise RuntimeError(f"nenhum pod pronto para {nome_deploy}")
    pod = pods[0]
    alvo, no = pod["metadata"]["name"], pod["spec"]["nodeName"]
    if tipo == "abrupta":
        # Crash do processo: SIGKILL no container, direto no nó (crictl stop -t 0).
        # O kubelet reinicia o container no MESMO pod. Obs.: "kubectl delete --force"
        # NÃO é abrupto: só apaga o objeto da API; o container segue vivo até o fim do
        # terminationGracePeriodSeconds (~30 s), atendendo conexões já abertas.
        cid = pod["status"]["containerStatuses"][0]["containerID"].split("://", 1)[1]
        cmd = ["ssh", no, f"sudo crictl --runtime-endpoint unix:///run/containerd/containerd.sock stop -t 0 {cid}"]
    elif tipo == "forcada":
        cmd = ["kubectl", "delete", "pod", alvo, "--wait=false", "--grace-period=0", "--force"]
    else:  # graciosa
        cmd = ["kubectl", "delete", "pod", alvo, "--wait=false"]
    t = agora()
    subprocess.run(cmd, check=True, capture_output=True, timeout=30)
    return alvo, seletor, no, t


def particionar(no, duracao):
    """Derruba a interface da rede privada do nó por `duracao` s (systemd-run: sobrevive ao fim do ssh)."""
    unidade = f"tcc-particao-{int(agora())}"
    script = f"ip link set {IFACE} down; sleep {duracao}; ip link set {IFACE} up"
    t = agora()
    subprocess.run(["ssh", no, f"sudo systemd-run --unit={unidade} --collect bash -c '{script}'"],
                   check=True, capture_output=True, timeout=20)
    return t


# ======================================================================
# Gráficos (opcional)
# ======================================================================

def grafico_falha(pasta, tl, prom, t_falha, marcas, ops):
    """marcas: instantes relativos à falha (s) para linhas verticais."""
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        print("[info] matplotlib ausente; gráfico não gerado (pip install matplotlib)")
        return
    x = [l["t_rel_s"] for l in tl]
    fig, eixos = plt.subplots(3, 1, figsize=(11, 9), sharex=True)
    for op in ops:
        eixos[0].plot(x, [l[f"{op}_ok"] for l in tl], label=f"{op} ok")
        eixos[0].plot(x, [l[f"{op}_falha"] for l in tl], "--", label=f"{op} falha")
        eixos[1].plot(x, [l[f"{op}_lat_media_s"] for l in tl], label=op)
    eixos[0].set_ylabel("transações/s")
    eixos[1].set_ylabel("latência média (s)")
    for serie, pontos in prom.get("altura_ledger", {}).items():
        xs, ys = [], []
        for i, (t, v) in enumerate(pontos):
            if i and t - pontos[i - 1][0] > 10:   # sem scrape: deixa lacuna, não interpola
                xs.append(float("nan")); ys.append(float("nan"))
            xs.append(t - t_falha); ys.append(v)
        eixos[2].plot(xs, ys, label=serie.replace("service=", ""))
    eixos[2].set_ylabel("altura do ledger")
    eixos[2].set_xlabel("tempo relativo à falha (s)")
    cores = {"falha": "red", "infra": "orange", "servico": "green"}
    for nome, t in marcas.items():
        if t is None or nome not in cores:
            continue
        for e in eixos:
            e.axvline(t, color=cores[nome], linestyle=":", linewidth=1)
    for e in eixos:
        e.legend(fontsize=7, loc="upper right")
        e.grid(alpha=0.3)
    fig.tight_layout()
    fig.savefig(pasta / "grafico.png", dpi=150)


def grafico_desempenho(pasta, linhas):
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        return
    fig, (a1, a2) = plt.subplots(1, 2, figsize=(11, 4))
    for op in ("emitir", "verificar", "revogar"):
        ls = [l for l in linhas if l["op"] == op]
        a1.plot([l["taxa_alvo_tps"] for l in ls], [l["throughput_tps"] for l in ls], "o-", label=op)
        a2.plot([l["taxa_alvo_tps"] for l in ls], [l["lat_media_s"] for l in ls], "o-", label=op)
    a1.set_xlabel("taxa enviada (TPS)"); a1.set_ylabel("throughput (TPS)")
    a2.set_xlabel("taxa enviada (TPS)"); a2.set_ylabel("latência média (s)")
    for a in (a1, a2):
        a.legend(); a.grid(alpha=0.3)
    fig.tight_layout()
    fig.savefig(pasta / "grafico.png", dpi=150)


# ======================================================================
# Execuções
# ======================================================================

def nova_pasta(nome):
    pasta = SAIDA / f"{nome}_{datetime.now():%Y%m%d-%H%M%S}"
    pasta.mkdir(parents=True)
    versoes = SAIDA.parent / "versoes.txt"
    if versoes.exists():
        shutil.copy(versoes, pasta / "versoes.txt")
    return pasta


def executar_desempenho(a):
    taxas = [int(x) for x in a.taxas.split(",")]
    pasta = nova_pasta("desempenho")
    bench = benchmarks.salvar(benchmarks.desempenho(taxas, a.duracao, a.workers), pasta / "benchmark.yaml")
    labels = [r["label"] for r in benchmarks.yaml.safe_load(open(bench))["test"]["rounds"]]

    print(f"[desempenho] {len(labels)} rounds; resultados em {pasta}")
    t_ini = agora()
    proc = iniciar_caliper(pasta, bench)
    finalizar_caliper(pasta, proc)
    t_fim = agora()

    txs = ler_transacoes(pasta)
    salvar_csv(pasta / "transacoes.csv", txs)
    linhas = []
    for i, label in enumerate(labels):
        op, taxa = label.rsplit("-", 1)
        linhas.append({"round": label, "op": op, "taxa_alvo_tps": int(taxa),
                       **estatisticas([t for t in txs if t["round"] == i])})
    salvar_csv(pasta / "desempenho.csv", linhas)
    coletar_prometheus(pasta, t_ini - 10, t_fim + 10, t_ini)
    grafico_desempenho(pasta, linhas)

    print(f"\n{'round':14} {'ok':>6} {'falha':>6} {'envio':>7} {'tput':>7} {'lat':>6} {'p95':>6}")
    for l in linhas:
        print(f"{l['round']:14} {l.get('sucesso', 0):6} {l.get('falhas', 0):6} "
              f"{l.get('taxa_envio_tps') or 0:7.1f} {l.get('throughput_tps') or 0:7.1f} "
              f"{l.get('lat_media_s') or 0:6.2f} {l.get('lat_p95_s') or 0:6.2f}")
    print(f"\nResultados em: {pasta}")


def executar_falha(a, rep):
    if a.tipo == "particao" and a.alvo not in ALVOS_NO:
        raise SystemExit(f"partição exige alvo de nó: {ALVOS_NO}")
    if a.tipo != "particao" and a.alvo not in ALVOS_POD:
        raise SystemExit(f"falha de pod exige alvo: {list(ALVOS_POD)}")

    print(f"\n[{rep}] aguardando ambiente saudável")
    esperar(ambiente_saudavel, limite=900, intervalo=5, desc="ambiente saudável")

    pasta = nova_pasta(f"{a.alvo}_{a.tipo}_r{rep}")
    bench = benchmarks.salvar(
        benchmarks.resiliencia(a.tps, a.duracao, a.populacao, a.workers, a.fracao_escrita),
        pasta / "benchmark.yaml")

    lock_ini = contar_lockups()
    monitor = MonitorK8s()
    monitor.start()
    proc = iniciar_caliper(pasta, bench)
    esperar_round(pasta, "resiliencia", proc)
    t_carga = agora()
    print(f"[{rep}] carga iniciada ({a.tps} TPS); baseline de {a.atraso}s")
    time.sleep(a.atraso)

    # --- falha ---
    info = {"alvo": a.alvo, "tipo": a.tipo}
    if a.tipo == "particao":
        t_falha = particionar(a.alvo, a.duracao_particao)
        info["no"] = a.alvo
        info["duracao_particao_s"] = a.duracao_particao
        print(f"[{rep}] partição de {a.alvo} por {a.duracao_particao}s")
    else:
        pod, seletor, no, t_falha = derrubar_pod(ALVOS_POD[a.alvo], a.tipo)
        info.update(pod_derrubado=pod, no=no)
        print(f"[{rep}] pod {pod} derrubado ({a.tipo})")

    finalizar_caliper(pasta, proc)
    t_fim = agora()

    # Espera a infraestrutura voltar (se ainda não voltou durante a carga)
    try:
        esperar(ambiente_saudavel, limite=600, intervalo=2, desc="recuperação")
    except TimeoutError:
        print(f"[{rep}] [aviso] ambiente não se recuperou em 10 min após a carga")
    time.sleep(3)
    monitor.parar.set()
    monitor.join(timeout=5)

    # --- infraestrutura ---
    if a.tipo == "particao":
        rto_inf, obs = rto_infra_no(monitor, a.alvo, t_falha)
        info.update(rto_infraestrutura_s=rto_inf, observacao_k8s=obs)
    else:
        novo, rto_inf, detectado = rto_infra_pod(monitor, pod, seletor, t_falha)
        info.update(pod_substituto=novo, rto_infraestrutura_s=rto_inf, ready_observado_s=detectado)
    salvar_csv(pasta / "eventos_k8s.csv",
               [{**e, "t_rel_s": round(e["t"] - t_falha, 1)} for e in monitor.eventos])

    # --- serviço ---
    txs = ler_transacoes(pasta)
    for t in txs:
        t["t_rel_criacao_s"] = round(t["criacao"] - t_falha, 3)
    salvar_csv(pasta / "transacoes.csv", txs)
    carga = [t for t in txs if t["round"] == 1]
    ops = sorted({t["op"] for t in carga})
    tl = linha_do_tempo(carga, t_falha, ops)
    salvar_csv(pasta / "linha_do_tempo.csv", tl)
    servico = {op: {**rto_servico([t for t in carga if t["op"] == op], t_falha),
                    **{k: v for k, v in estatisticas([t for t in carga if t["op"] == op]).items()
                       if k in ("sucesso", "falhas")}}
               for op in ops}
    rtos = [s["rto_servico_s"] for s in servico.values() if s["rto_servico_s"] is not None]

    # --- Prometheus ---
    prom = coletar_prometheus(pasta, t_carga - 30, t_fim + 30, t_falha)
    div, conv = convergencia_ledger(prom.get("altura_ledger", {}), t_falha)

    inesperados = nos_inesperados(monitor, t_carga, a.alvo if a.tipo == "particao" else None)
    lock_fim = contar_lockups()
    lockups = {n: (lock_fim[n] - lock_ini[n]) for n in ALVOS_NO
               if lock_ini.get(n) is not None and lock_fim.get(n) is not None
               and lock_fim[n] > lock_ini[n]}
    resumo = {
        **info,
        "inicio_carga_utc": datetime.fromtimestamp(t_carga, timezone.utc).isoformat(),
        "falha_utc": datetime.fromtimestamp(t_falha, timezone.utc).isoformat(),
        "parametros": {"tps": a.tps, "duracao_s": a.duracao, "atraso_s": a.atraso,
                       "populacao": a.populacao, "workers": a.workers, "fracao_escrita": a.fracao_escrita},
        "rto_servico_s": max(rtos) if rtos else None,
        "servico_por_fluxo": servico,
        "divergencia_max_blocos": div,
        "convergencia_ledger_s": conv,
        "nos_notready_inesperados": inesperados,
        "soft_lockups_durante": lockups,
        "execucao_valida": not inesperados and not lockups and proc.returncode == 0,
    }
    (pasta / "resumo.json").write_text(json.dumps(resumo, indent=2, ensure_ascii=False))
    grafico_falha(pasta, tl, prom, t_falha, {"falha": 0, "infra": resumo["rto_infraestrutura_s"],
                                    "servico": resumo["rto_servico_s"]}, ops)

    print(json.dumps(resumo, indent=2, ensure_ascii=False))
    if inesperados:
        print(f"[{rep}] [AVISO] nós NotReady fora do plano: {inesperados}. Descarte esta execução.")
    if lockups:
        print(f"[{rep}] [AVISO] soft lockup durante a execução: {lockups}. Descarte esta execução.")
    print(f"Resultados em: {pasta}")


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="modo", required=True)

    d = sub.add_parser("desempenho", help="RQ1: taxas crescentes")
    d.add_argument("--taxas", default="10,25,50,100,150")
    d.add_argument("--duracao", type=int, default=30, help="segundos por taxa")
    d.add_argument("--workers", type=int, default=2)

    f = sub.add_parser("falha", help="RQ2/RQ3: carga contínua + falha")
    f.add_argument("alvo", choices=list(ALVOS_POD) + ALVOS_NO)
    f.add_argument("--tipo", choices=["graciosa", "abrupta", "forcada", "particao"], required=True,
                   help="graciosa: kubectl delete; abrupta: SIGKILL no container (crash); "
                        "forcada: delete --force (só API); particao: rede do nó")
    f.add_argument("--tps", type=int, default=25, help="taxa da carga (~50%% da saturação da RQ1)")
    f.add_argument("--duracao", type=int, default=300, help="duração do round de carga (s)")
    f.add_argument("--atraso", type=int, default=60, help="baseline antes da falha (s)")
    f.add_argument("--duracao-particao", type=int, default=60)
    f.add_argument("--populacao", type=int, default=500, help="CPFs consultados por worker")
    f.add_argument("--fracao-escrita", type=float, default=0.5)
    f.add_argument("--workers", type=int, default=2)
    f.add_argument("--repeticoes", type=int, default=1)
    f.add_argument("--intervalo", type=int, default=120, help="pausa entre repetições (s)")
    a = p.parse_args()

    if a.modo == "desempenho":
        executar_desempenho(a)
        return
    for rep in range(1, a.repeticoes + 1):
        executar_falha(a, rep)
        if rep < a.repeticoes:
            print(f"pausa de {a.intervalo}s antes da próxima repetição")
            time.sleep(a.intervalo)


if __name__ == "__main__":
    main()