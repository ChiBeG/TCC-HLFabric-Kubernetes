'use strict';
/*
 * Base comum dos workloads do vínculo estudantil.
 *
 * CPF determinístico (11 dígitos) = série (5) + worker (1) + índice (5).
 *   A série vem de roundArguments.serie; rounds com a mesma série atuam sobre
 *   os mesmos CPFs (ex.: emitir série 101, depois verificar série 101).
 *   Série 00000 é reservada para testes manuais (08-smoke-test.sh).
 *
 * Log por transação: se a variável CALIPER_TXLOG estiver definida, cada worker
 * grava <CALIPER_TXLOG>.w<N>.csv com uma linha por transação:
 *   round,op,org,cpf,status,t_criacao_ms,t_final_ms
 * O relatório do Caliper agrega o round inteiro; este log permite reconstruir
 * a linha do tempo durante a falha (experimento.py).
 */
const fs = require('fs');
const { WorkloadModuleBase } = require('@hyperledger/caliper-core');

const ORGS = {
    org1: { invokerMspId: 'Org1MSP', invokerIdentity: 'caliper' },
    org2: { invokerMspId: 'Org2MSP', invokerIdentity: 'caliper' },
};

class VinculoBase extends WorkloadModuleBase {
    constructor() {
        super();
        this.txIndex = 0;
        this.txlog = null;
    }

    async initializeWorkloadModule(workerIndex, totalWorkers, roundIndex, roundArguments, sutAdapter, sutContext) {
        await super.initializeWorkloadModule(workerIndex, totalWorkers, roundIndex, roundArguments, sutAdapter, sutContext);
        const args = roundArguments || {};
        this.serie = Number(args.serie || 1);
        this.instituicao = args.instituicao || 'UNIRIO';
        this.validade = args.validade || '2027-12-31';
        // Tamanho do conjunto de CPFs já emitidos que pode ser consultado (verificação cíclica)
        this.populacao = Number(args.populacao || 0);
        this.txIndex = 0;
        if (workerIndex > 9) {
            throw new Error('o esquema de CPF suporta no máximo 10 workers');
        }
        if (process.env.CALIPER_TXLOG) {
            this.txlog = `${process.env.CALIPER_TXLOG}.w${workerIndex}.csv`;
            if (!fs.existsSync(this.txlog)) {
                fs.writeFileSync(this.txlog, 'round,op,org,cpf,status,t_criacao_ms,t_final_ms\n');
            }
        }
    }

    cpf(serie, indice) {
        return String(serie).padStart(5, '0') + String(this.workerIndex) + String(indice).padStart(5, '0');
    }

    proximoCpf() {
        this.txIndex++;
        if (this.txIndex > 99999) {
            throw new Error('índice de CPF esgotado na série; use outra série');
        }
        return this.cpf(this.serie, this.txIndex);
    }

    // Envia a transação e registra o resultado; nunca lança (falha vira linha "failed").
    async enviar(op, org, funcao, args, readOnly) {
        const inicio = Date.now();
        let status = 'failed', tCriacao = inicio, tFinal = 0;
        try {
            const r = await this.sutAdapter.sendRequests({
                contractId: 'vinculo',
                contractFunction: funcao,
                contractArguments: args,
                readOnly,
                ...ORGS[org],
            });
            status = r.GetStatus();
            tCriacao = r.GetTimeCreate() || inicio;
            tFinal = r.GetTimeFinal();
        } catch (e) {
            // erro antes de chegar à rede (ex.: conexão); o Caliper não conta, mas o log registra
        }
        if (!tFinal) {
            tFinal = Date.now();
        }
        if (this.txlog) {
            fs.appendFileSync(this.txlog, `${this.roundIndex},${op},${org},${args[0]},${status},${tCriacao},${tFinal}\n`);
        }
    }

    emitir(cpf) {
        return this.enviar('emitir', 'org1', 'EmitirVinculo', [cpf, this.instituicao, this.validade], false);
    }

    verificar(cpf) {
        return this.enviar('verificar', 'org2', 'VerificarVinculo', [cpf], true);
    }

    revogar(cpf) {
        return this.enviar('revogar', 'org1', 'RevogarVinculo', [cpf, this.instituicao], false);
    }
}

module.exports = VinculoBase;
