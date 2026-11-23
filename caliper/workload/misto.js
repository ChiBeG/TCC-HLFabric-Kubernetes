'use strict';
/*
 * Carga mista para os experimentos de resiliência: alterna
 *   - emissão de CPF novo pela Org1 (série = roundArguments.serie), e
 *   - verificação pela Org2 de CPFs já emitidos (série = roundArguments.serieConsulta,
 *     ciclando entre 1 e roundArguments.populacao).
 * Assim a falha de qualquer componente (peer da Org1, peer da Org2, orderer,
 * chaincode) aparece em pelo menos um dos dois fluxos.
 * roundArguments.fracaoEscrita (0..1, padrão 0.5) define a proporção de emissões.
 */
const VinculoBase = require('./base');

class Misto extends VinculoBase {
    async initializeWorkloadModule(...args) {
        await super.initializeWorkloadModule(...args);
        const ra = args[3] || {};
        this.serieConsulta = Number(ra.serieConsulta || 1);
        this.fracaoEscrita = ra.fracaoEscrita === undefined ? 0.5 : Number(ra.fracaoEscrita);
        if (this.populacao <= 0) {
            throw new Error('misto.js requer roundArguments.populacao > 0');
        }
        this.contEscrita = 0;
        this.contLeitura = 0;
    }

    async submitTransaction() {
        const total = this.contEscrita + this.contLeitura;
        // Distribuição determinística: mantém a fração de escrita ao longo do tempo
        if (this.contEscrita < this.fracaoEscrita * (total + 1)) {
            this.contEscrita++;
            await this.emitir(this.proximoCpf());
        } else {
            this.contLeitura++;
            const i = ((this.contLeitura - 1) % this.populacao) + 1;
            await this.verificar(this.cpf(this.serieConsulta, i));
        }
    }
}

module.exports.createWorkloadModule = () => new Misto();
