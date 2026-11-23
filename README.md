# Hyperledger Fabric sobre Kubernetes: verificação de vínculo estudantil

Código da prova de conceito do Trabalho de Conclusão de Curso
"Implementação de uma Prova de Conceito de Identidade Digital em Blockchain
Utilizando Containers" (Bacharelado em Sistemas de Informação, UNIRIO).

Cria do zero um cluster Kubernetes de três máquinas virtuais, implanta uma rede
Hyperledger Fabric com duas organizações (Universidade e Verificador) por meio do
HLF Operator, publica o chaincode de vínculo estudantil e executa experimentos de
desempenho e de resiliência com o Hyperledger Caliper.

## Estrutura

| Pasta / arquivo | Conteúdo |
|---|---|
| `Vagrantfile` | Criação das 3 VMs (VirtualBox) |
| `config.env` | Configuração central: IPs, versões, nomes e credenciais de laboratório |
| `scripts/` | Etapas 01 a 09 da implantação e `reset-ledger.sh` |
| `chaincode/vinculo/` | Chaincode em Go (emitir, verificar, revogar, histórico) |
| `caliper/` | Rede e workloads do Caliper, `experimento.py` e `consolida.py` |
| `artifacts/resultados/` | Resumos dos experimentos e resultados consolidados |

## Requisitos

- VirtualBox e Vagrant
- ~18 GB de RAM livres para as VMs

## Como executar

```bash
vagrant up                          # etapas 01 a 03: VMs e cluster
vagrant ssh control-plane
cd /vagrant
bash scripts/04-addons.sh           # Helm, armazenamento, Prometheus/Grafana, MetalLB, Istio
bash scripts/05-fabric-base.sh      # HLF Operator, CAs e identidades
bash scripts/06-fabric-network.sh   # peers, orderer e canal
bash scripts/07-chaincode.sh        # chaincode como serviço
bash scripts/08-smoke-test.sh       # validação funcional
bash scripts/09-caliper.sh          # Caliper e carga de teste
```

Para zerar o ledger e recriar a rede: `bash scripts/reset-ledger.sh`.

## Experimentos

```bash
cd ~/caliper-vinculo
./experimento.py desempenho --taxas 10,25,50,100                 # desempenho
./experimento.py falha peer-org1 --tipo abrupta --repeticoes 3   # resiliência
./consolida.py --desde <AAAA-MM-DDTHH:MM>                        # tabelas e gráficos
```

Alvos de falha: `peer-org1`, `peer-org2`, `orderer`, `chaincode`.
Tipos: `abrupta`, `graciosa`, `forcada`.

## Observação

As credenciais em `config.env` são apenas de laboratório. Chaves, tokens e
kubeconfig gerados durante a implantação não são versionados.