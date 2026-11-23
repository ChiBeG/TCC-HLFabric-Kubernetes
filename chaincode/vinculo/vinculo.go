// Chaincode de vínculo estudantil (prova de conceito do TCC).
//
// Executado como serviço externo (CCaaS): o processo escuta em
// CHAINCODE_SERVER_ADDRESS e o peer se conecta a ele.
//
// Regras:
//   - Somente a organização emissora (Org1MSP) emite e revoga vínculos.
//   - Não é possível emitir para um CPF que já possui vínculo ativo.
//   - A revogação só é aceita para a mesma instituição que emitiu o vínculo.
//   - Qualquer organização do canal pode verificar e consultar o histórico.
package main

import (
	"encoding/json"
	"fmt"
	"os"
	"time"

	"github.com/hyperledger/fabric-chaincode-go/shim"
	"github.com/hyperledger/fabric-contract-api-go/contractapi"
)

const (
	mspEmissor     = "Org1MSP"
	statusAtivo    = "ativo"
	statusRevogado = "revogado"
)

type VinculoContract struct {
	contractapi.Contract
}

type Vinculo struct {
	CPF         string `json:"cpf"`
	Instituicao string `json:"instituicao"`
	Validade    string `json:"validade"`
	Status      string `json:"status"`
}

type RegistroHistorico struct {
	TxID      string   `json:"txId"`
	Timestamp string   `json:"timestamp"`
	Removido  bool     `json:"removido"`
	Vinculo   *Vinculo `json:"vinculo,omitempty"`
}

func exigirEmissor(ctx contractapi.TransactionContextInterface) error {
	msp, err := ctx.GetClientIdentity().GetMSPID()
	if err != nil {
		return fmt.Errorf("não foi possível identificar a organização: %v", err)
	}
	if msp != mspEmissor {
		return fmt.Errorf("operação restrita à organização emissora (%s); solicitante: %s", mspEmissor, msp)
	}
	return nil
}

func ler(ctx contractapi.TransactionContextInterface, cpf string) (*Vinculo, error) {
	dado, err := ctx.GetStub().GetState(cpf)
	if err != nil {
		return nil, fmt.Errorf("erro ao consultar o world state: %v", err)
	}
	if dado == nil {
		return nil, nil
	}
	var v Vinculo
	if err := json.Unmarshal(dado, &v); err != nil {
		return nil, err
	}
	return &v, nil
}

func gravar(ctx contractapi.TransactionContextInterface, v *Vinculo) error {
	dado, err := json.Marshal(v)
	if err != nil {
		return err
	}
	return ctx.GetStub().PutState(v.CPF, dado)
}

// EmitirVinculo registra um vínculo ativo para o CPF.
func (c *VinculoContract) EmitirVinculo(ctx contractapi.TransactionContextInterface,
	cpf string, instituicao string, validade string) error {
	if err := exigirEmissor(ctx); err != nil {
		return err
	}
	if _, err := time.Parse("2006-01-02", validade); err != nil {
		return fmt.Errorf("validade inválida (use AAAA-MM-DD): %s", validade)
	}
	atual, err := ler(ctx, cpf)
	if err != nil {
		return err
	}
	if atual != nil && atual.Status == statusAtivo {
		return fmt.Errorf("CPF %s já possui vínculo ativo", cpf)
	}
	return gravar(ctx, &Vinculo{CPF: cpf, Instituicao: instituicao, Validade: validade, Status: statusAtivo})
}

// VerificarVinculo retorna o estado atual do vínculo do CPF.
func (c *VinculoContract) VerificarVinculo(ctx contractapi.TransactionContextInterface,
	cpf string) (*Vinculo, error) {
	v, err := ler(ctx, cpf)
	if err != nil {
		return nil, err
	}
	if v == nil {
		return nil, fmt.Errorf("vínculo não encontrado para o CPF %s", cpf)
	}
	return v, nil
}

// RevogarVinculo marca o vínculo como revogado.
func (c *VinculoContract) RevogarVinculo(ctx contractapi.TransactionContextInterface,
	cpf string, instituicao string) error {
	if err := exigirEmissor(ctx); err != nil {
		return err
	}
	v, err := ler(ctx, cpf)
	if err != nil {
		return err
	}
	if v == nil {
		return fmt.Errorf("vínculo não encontrado para o CPF %s", cpf)
	}
	if v.Instituicao != instituicao {
		return fmt.Errorf("vínculo emitido por %s, não por %s", v.Instituicao, instituicao)
	}
	if v.Status == statusRevogado {
		return fmt.Errorf("vínculo do CPF %s já está revogado", cpf)
	}
	v.Status = statusRevogado
	return gravar(ctx, v)
}

// HistoricoVinculo retorna todas as versões registradas no ledger para o CPF.
func (c *VinculoContract) HistoricoVinculo(ctx contractapi.TransactionContextInterface,
	cpf string) ([]RegistroHistorico, error) {
	it, err := ctx.GetStub().GetHistoryForKey(cpf)
	if err != nil {
		return nil, err
	}
	defer it.Close()

	historico := []RegistroHistorico{}
	for it.HasNext() {
		r, err := it.Next()
		if err != nil {
			return nil, err
		}
		reg := RegistroHistorico{TxID: r.TxId, Removido: r.IsDelete}
		if r.Timestamp != nil {
			reg.Timestamp = r.Timestamp.AsTime().UTC().Format(time.RFC3339Nano)
		}
		if !r.IsDelete {
			var v Vinculo
			if err := json.Unmarshal(r.Value, &v); err == nil {
				reg.Vinculo = &v
			}
		}
		historico = append(historico, reg)
	}
	return historico, nil
}

func main() {
	cc, err := contractapi.NewChaincode(new(VinculoContract))
	if err != nil {
		panic(err)
	}
	servidor := &shim.ChaincodeServer{
		CCID:     os.Getenv("CHAINCODE_ID"),
		Address:  os.Getenv("CHAINCODE_SERVER_ADDRESS"),
		CC:       cc,
		TLSProps: shim.TLSProperties{Disabled: true},
	}
	if err := servidor.Start(); err != nil {
		panic(err)
	}
}
