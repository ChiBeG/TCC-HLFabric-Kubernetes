#!/usr/bin/env python3
"""Consolida os resultados da campanha em tabelas (CSV e LaTeX) e gráficos.

Uso (no control-plane):
    ./consolida.py --desde 2026-11-21T12:15 [--desempenho <pasta>] [--saida <pasta>]

--desde       considera só execuções de falha com falha_utc >= este instante (UTC)
--desempenho  pasta de um teste de desempenho (desempenho_*) para a tabela da RQ1
Execuções com execucao_valida=false são listadas e descartadas.
"""
import argparse
import csv
import json
import statistics as st
from datetime import datetime, timezone
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

RES = Path("/vagrant/artifacts/resultados")
ORDEM_ALVO = ["peer-org1", "peer-org2", "orderer", "chaincode"]
NOME_ALVO = {"peer-org1": "Peer Universidade", "peer-org2": "Peer Verificador",
             "orderer": "Orderer", "chaincode": "Chaincode"}
ORDEM_TIPO = ["abrupta", "graciosa", "forcada"]
NOME_TIPO = {"abrupta": "Abrupta", "graciosa": "Graciosa", "forcada": "Forçada"}


def ts(iso):
    d = datetime.fromisoformat(iso)
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def media_dp(vals):
    vals = [v for v in vals if v is not None]
    if not vals:
        return None, None, 0
    return st.mean(vals), (st.stdev(vals) if len(vals) > 1 else 0.0), len(vals)


def fmt(m, d):
    if m is None:
        return "--"
    return f"{m:.1f} $\\pm$ {d:.1f}".replace(".", ",")


def carregar(desde, res=RES, excluir=()):
    execs, descartadas = [], []
    for f in sorted(res.glob("*/resumo.json")):
        r = json.loads(f.read_text())
        if "falha_utc" not in r or ts(r["falha_utc"]) < desde:
            continue
        r["_pasta"] = f.parent.name
        if any(e in r["_pasta"] for e in excluir):
            r["_motivo"] = "excluída manualmente"
            descartadas.append(r)
            continue
        (execs if r.get("execucao_valida") else descartadas).append(r)
    return execs, descartadas


def agrupar(execs):
    g = {}
    for r in execs:
        g.setdefault((r["alvo"], r["tipo"]), []).append(r)
    chaves = sorted(g, key=lambda k: (ORDEM_TIPO.index(k[1]) if k[1] in ORDEM_TIPO else 9,
                                      ORDEM_ALVO.index(k[0]) if k[0] in ORDEM_ALVO else 9))
    return [(k, g[k]) for k in chaves]


def fluxo(r, op, campo):
    return (r.get("servico_por_fluxo", {}).get(op) or {}).get(campo)


def tabela_resiliencia(grupos, saida):
    linhas, tex = [], []
    for (alvo, tipo), rs in grupos:
        lin = {"alvo": alvo, "tipo": tipo, "n": len(rs)}
        for nome, get in [
            ("rto_infra", lambda r: r.get("rto_infraestrutura_s")),
            ("rto_emitir", lambda r: fluxo(r, "emitir", "rto_servico_s")),
            ("rto_verificar", lambda r: fluxo(r, "verificar", "rto_servico_s")),
            ("falhas_emitir", lambda r: fluxo(r, "emitir", "falhas_apos_falha")),
            ("falhas_verificar", lambda r: fluxo(r, "verificar", "falhas_apos_falha")),
            ("divergencia", lambda r: r.get("divergencia_max_blocos")),
            ("convergencia", lambda r: r.get("convergencia_ledger_s")),
        ]:
            m, d, _ = media_dp([get(r) for r in rs])
            lin[f"{nome}_media"], lin[f"{nome}_dp"] = m, d
        linhas.append(lin)
        tex.append(" & ".join([
            NOME_TIPO.get(tipo, tipo), NOME_ALVO.get(alvo, alvo), str(len(rs)),
            fmt(lin["rto_infra_media"], lin["rto_infra_dp"]),
            fmt(lin["rto_emitir_media"], lin["rto_emitir_dp"]),
            fmt(lin["rto_verificar_media"], lin["rto_verificar_dp"]),
            fmt(lin["falhas_emitir_media"], lin["falhas_emitir_dp"]),
            fmt(lin["falhas_verificar_media"], lin["falhas_verificar_dp"]),
        ]) + " \\\\")

    with open(saida / "resiliencia.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(linhas[0]))
        w.writeheader()
        w.writerows(linhas)
    (saida / "resiliencia.tex").write_text(
        "% Tipo & Alvo & n & RTO infra (s) & RTO emissão (s) & RTO verificação (s) "
        "& falhas emissão & falhas verificação\n" + "\n".join(tex) + "\n")


def grafico_rto(grupos, saida):
    """Barras com a média e pontos com cada execução (n pequeno: o desvio-padrão engana)."""
    rot, infra, serv = [], [], []
    for (alvo, tipo), rs in grupos:
        rot.append(f"{NOME_ALVO.get(alvo, alvo)}\n({NOME_TIPO.get(tipo, tipo)})")
        infra.append([r.get("rto_infraestrutura_s") for r in rs if r.get("rto_infraestrutura_s") is not None])
        serv.append([max([v for v in (fluxo(r, "emitir", "rto_servico_s"), fluxo(r, "verificar", "rto_servico_s"))
                          if v is not None] or [0]) for r in rs])
    desenhar_rto(rot, infra, serv, saida)


def desenhar_rto(rot, infra, serv, saida):
    x = list(range(len(rot)))
    w = 0.38
    fig, ax = plt.subplots(figsize=(8, 4))
    for desl, vals, cor, nome in ((-w / 2, infra, "#7a8ca3", "Infraestrutura"),
                                  (w / 2, serv, "#c0603a", "Serviço")):
        ax.bar([i + desl for i in x], [st.mean(v) if v else 0 for v in vals], w, color=cor, label=nome)
        for i, v in enumerate(vals):
            ax.scatter([i + desl] * len(v), v, s=14, color="black", zorder=3)
    ax.set_xticks(x)
    ax.set_xticklabels(rot, fontsize=8)
    ax.set_ylabel("Tempo de recuperação (s)")
    ax.legend(frameon=False)
    ax.spines[["top", "right"]].set_visible(False)
    ax.grid(axis="y", alpha=0.3)
    fig.tight_layout()
    for ext in ("pdf", "png"):
        fig.savefig(saida / f"rto_infra_servico.{ext}", dpi=200)
    plt.close(fig)


def tabela_desempenho(pasta, saida):
    linhas = list(csv.DictReader(open(pasta / "desempenho.csv")))
    nome = {"emitir": "Emissão", "verificar": "Verificação", "revogar": "Revogação"}
    tex = []
    for l in linhas:
        def n(c, casas=2):
            v = l.get(c)
            return "--" if v in (None, "") else f"{float(v):.{casas}f}".replace(".", ",")
        tex.append(f"{nome.get(l['op'], l['op'])} & {l['taxa_alvo_tps']} & {l.get('sucesso', '')} & "
                   f"{l.get('falhas', '')} & {n('throughput_tps', 1)} & {n('lat_media_s')} & {n('lat_p95_s')} \\\\")
    (saida / "desempenho.tex").write_text(
        "% Operação & Taxa (tps) & Sucesso & Falha & Throughput (tps) & Latência média (s) & p95 (s)\n"
        + "\n".join(tex) + "\n")

    fig, (a1, a2) = plt.subplots(1, 2, figsize=(9, 3.5))
    for op in ("emitir", "verificar", "revogar"):
        ls = [l for l in linhas if l["op"] == op]
        x = [int(l["taxa_alvo_tps"]) for l in ls]
        a1.plot(x, [float(l["throughput_tps"] or 0) for l in ls], "o-", label=nome[op])
        a2.plot(x, [float(l["lat_media_s"] or 0) for l in ls], "o-", label=nome[op])
    a1.plot([0, max(x)], [0, max(x)], ":", color="gray", lw=1)
    a1.set(xlabel="Taxa enviada (tps)", ylabel="Throughput (tps)")
    a2.set(xlabel="Taxa enviada (tps)", ylabel="Latência média (s)")
    for a in (a1, a2):
        a.spines[["top", "right"]].set_visible(False)
        a.grid(alpha=0.3)
    a2.legend(frameon=False)
    fig.tight_layout()
    for ext in ("pdf", "png"):
        fig.savefig(saida / f"desempenho.{ext}", dpi=200)
    plt.close(fig)


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--desde", required=True, help="instante UTC, ex.: 2026-11-21T12:15")
    p.add_argument("--desempenho", type=Path)
    p.add_argument("--saida", type=Path, default=RES / "consolidado")
    p.add_argument("--resultados", type=Path, default=RES)
    p.add_argument("--excluir", nargs="*", default=[], help="pastas (ou trechos do nome) a descartar")
    a = p.parse_args()
    a.saida.mkdir(parents=True, exist_ok=True)

    execs, descartadas = carregar(ts(a.desde), a.resultados, a.excluir)
    grupos = agrupar(execs)
    print(f"{len(execs)} execuções válidas, {len(descartadas)} descartadas")
    for r in descartadas:
        print(f"  descartada: {r['_pasta']} ({(r.get('_motivo') + '; ') if r.get('_motivo') else ''}lockups={r.get('soft_lockups_durante')}, "
              f"nós={r.get('nos_notready_inesperados')})")
    for (alvo, tipo), rs in grupos:
        print(f"  {tipo:9} {alvo:10} n={len(rs)}  " +
              "  ".join(f"{r['_pasta']}" for r in rs))
    if grupos:
        tabela_resiliencia(grupos, a.saida)
        grafico_rto(grupos, a.saida)
    if a.desempenho:
        tabela_desempenho(a.desempenho, a.saida)
    print(f"Saída em: {a.saida}")


if __name__ == "__main__":
    main()