'use strict';
// Org1 emite um vínculo novo por transação (escrita: endosso, ordenação, commit).
const VinculoBase = require('./base');

class Emitir extends VinculoBase {
    async submitTransaction() {
        await this.emitir(this.proximoCpf());
    }
}

module.exports.createWorkloadModule = () => new Emitir();
