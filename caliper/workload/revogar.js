'use strict';
// Org1 revoga os vínculos emitidos na mesma série (escrita).
const VinculoBase = require('./base');

class Revogar extends VinculoBase {
    async submitTransaction() {
        await this.revogar(this.proximoCpf());
    }
}

module.exports.createWorkloadModule = () => new Revogar();
