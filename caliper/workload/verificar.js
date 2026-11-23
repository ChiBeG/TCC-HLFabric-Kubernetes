'use strict';
// Org2 consulta vínculos (leitura: só o peer, sem orderer).
// Percorre os CPFs da série na mesma ordem em que foram emitidos;
// com roundArguments.populacao > 0, volta ao início ao chegar nesse número.
const VinculoBase = require('./base');

class Verificar extends VinculoBase {
    async submitTransaction() {
        this.txIndex++;
        const i = this.populacao > 0 ? ((this.txIndex - 1) % this.populacao) + 1 : this.txIndex;
        await this.verificar(this.cpf(this.serie, i));
    }
}

module.exports.createWorkloadModule = () => new Verificar();
