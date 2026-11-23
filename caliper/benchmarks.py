#!/usr/bin/env python3
"""
Gera os arquivos de benchmark do Caliper (importado por experimento.py; também roda sozinho).

Séries de CPF: cada execução reserva séries novas em serie.txt, para nunca reemitir
um CPF já usado (o chaincode rejeita emissão para CPF com vínculo ativo).

  benchmarks.py desempenho [--taxas 10,25,50,100,150] [--duracao 30] [--workers 2]
  benchmarks.py resiliencia [--tps 25] [--duracao 300] [--populacao 500] [--workers 2]
  benchmarks.py fumaca
"""
import argparse
from pathlib import Path

import yaml

AQUI = Path(__file__).resolve().parent
ARQ_SERIE = AQUI / "serie.txt"


def reservar_series(n):
    """Reserva n séries consecutivas e devolve a primeira (série 0 é dos testes manuais)."""
    atual = int(ARQ_SERIE.read_text()) if ARQ_SERIE.exists() else 100
    if atual + n > 99999:
        raise SystemExit("séries esgotadas: limpe o ledger (scripts/reset-ledger.sh) e apague serie.txt")
    ARQ_SERIE.write_text(str(atual + n))
    return atual


def taxa_fixa(tps):
    return {"type": "fixed-rate", "opts": {"tps": tps}}


def rodada(label, modulo, args, tps, tx=None, duracao=None):
    r = {"label": label, "rateControl": taxa_fixa(tps),
         "workload": {"module": f"workload/{modulo}.js", "arguments": args}}
    if tx is not None:
        r["txNumber"] = tx
    else:
        r["txDuration"] = duracao
    return r


def benchmark(nome, workers, rounds):
    return {"test": {"name": nome, "workers": {"number": workers}, "rounds": rounds}}


def desempenho(taxas, duracao, workers):
    """RQ1: taxa crescente para emitir, verificar e revogar.
    Verificação lê os CPFs emitidos na menor taxa (todos devem existir), em ciclo."""
    base = reservar_series(len(taxas))
    series = {t: base + i for i, t in enumerate(taxas)}
    populacao = taxas[0] * duracao // workers
    rounds = []
    for t in taxas:
        rounds.append(rodada(f"emitir-{t}", "emitir", {"serie": series[t]}, t, tx=t * duracao))
    for t in taxas:
        rounds.append(rodada(f"verificar-{t}", "verificar",
                             {"serie": series[taxas[0]], "populacao": populacao}, t, tx=t * duracao))
    for t in taxas:
        rounds.append(rodada(f"revogar-{t}", "revogar", {"serie": series[t]}, t, tx=t * duracao))
    return benchmark("desempenho-vinculo", workers, rounds)


def resiliencia(tps, duracao, populacao, workers, fracao_escrita=0.5):
    """RQ2/RQ3: round de preparação (emite a população consultada) + um round longo misto,
    sem intervalos, durante o qual a falha é injetada."""
    prep = reservar_series(2)
    carga = prep + 1
    rounds = [
        rodada("preparacao", "emitir", {"serie": prep}, 25, tx=populacao * workers),
        rodada("resiliencia", "misto",
               {"serie": carga, "serieConsulta": prep, "populacao": populacao,
                "fracaoEscrita": fracao_escrita}, tps, duracao=duracao),
    ]
    return benchmark("resiliencia-vinculo", workers, rounds)


def fumaca(n=50, tps=10):
    """Validação rápida da integração (09-caliper.sh): 1 worker, emitir/verificar/revogar."""
    s = reservar_series(1)
    return benchmark("fumaca-vinculo", 1, [
        rodada("emitir", "emitir", {"serie": s}, tps, tx=n),
        rodada("verificar", "verificar", {"serie": s}, tps, tx=n),
        rodada("revogar", "revogar", {"serie": s}, tps, tx=n),
    ])


def salvar(bench, arquivo):
    Path(arquivo).parent.mkdir(parents=True, exist_ok=True)
    yaml.safe_dump(bench, open(arquivo, "w"), sort_keys=False, allow_unicode=True)
    return arquivo


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="tipo", required=True)
    d = sub.add_parser("desempenho")
    d.add_argument("--taxas", default="10,25,50,100,150")
    d.add_argument("--duracao", type=int, default=30)
    d.add_argument("--workers", type=int, default=2)
    r = sub.add_parser("resiliencia")
    r.add_argument("--tps", type=int, default=25)
    r.add_argument("--duracao", type=int, default=300)
    r.add_argument("--populacao", type=int, default=500)
    r.add_argument("--workers", type=int, default=2)
    sub.add_parser("fumaca")
    a = p.parse_args()
    if a.tipo == "fumaca":
        b = fumaca()
    elif a.tipo == "desempenho":
        b = desempenho([int(x) for x in a.taxas.split(",")], a.duracao, a.workers)
    else:
        b = resiliencia(a.tps, a.duracao, a.populacao, a.workers)
    print(salvar(b, AQUI / "benchmarks" / f"{a.tipo}.yaml"))
