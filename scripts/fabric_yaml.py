#!/usr/bin/env python3
"""
Ajusta e gera os YAMLs declarativos da rede Fabric.

  fabric_yaml.py no <arquivo.yaml> <worker>   -> afinidade de nó + ServiceMonitor
  fabric_yaml.py canal <saida_dir>            -> FabricMainChannel + FabricFollowerChannels

Decisões registradas (ver desvios no README):
- nodeSelector do HLF Operator 1.14.0 tem bug (formato incompatível com o Pod);
  usamos spec.affinity, que o operator repassa corretamente.
- O canal usa endpoints INTERNOS (Services do K8s) para peer↔orderer e gossip,
  evitando passar pelo ingress do Istio.
"""
import os
import subprocess
import sys

import yaml

CANAL = os.environ.get("CANAL", "canal-vinculo")


def no(arquivo, worker):
    d = yaml.safe_load(open(arquivo))
    spec = d["spec"]
    spec.pop("nodeSelector", None)
    spec["affinity"] = {"nodeAffinity": {"requiredDuringSchedulingIgnoredDuringExecution": {
        "nodeSelectorTerms": [{"matchExpressions": [{
            "key": "kubernetes.io/hostname", "operator": "In", "values": [worker]}]}]}}}
    spec["serviceMonitor"] = {"enabled": True, "interval": "5s", "scrapeTimeout": "4s",
                              "sampleLimit": 0, "labels": {"release": "prometheus"}}
    yaml.safe_dump(d, open(arquivo, "w"), sort_keys=False)
    print(f"{arquivo}: afinidade={worker}, serviceMonitor=5s")


def tls_orderer():
    return subprocess.check_output(
        ["kubectl", "get", "fabricorderernodes", "ord-node1",
         "-o", "jsonpath={.status.tlsCert}"], text=True)


def identidade(secret):
    return {"secretName": secret, "secretKey": "user.yaml", "secretNamespace": "default"}


def canal(saida):
    tls = tls_orderer()
    if "BEGIN CERTIFICATE" not in tls:
        sys.exit("erro: certificado TLS do orderer indisponível")

    principal = {
        "apiVersion": "hlf.kungfusoftware.es/v1alpha1",
        "kind": "FabricMainChannel",
        "metadata": {"name": CANAL},
        "spec": {
            "name": CANAL,
            "adminOrdererOrganizations": [{"mspID": "OrdererMSP"}],
            "adminPeerOrganizations": [{"mspID": "Org1MSP"}, {"mspID": "Org2MSP"}],
            "identities": {
                "OrdererMSP": identidade("ord-admin-tls"),
                "OrdererMSP-sign": identidade("ord-admin-sign"),
                "Org1MSP": identidade("org1-admin"),
                "Org2MSP": identidade("org2-admin"),
            },
            "peerOrganizations": [
                {"mspID": "Org1MSP", "caName": "org1-ca", "caNamespace": "default"},
                {"mspID": "Org2MSP", "caName": "org2-ca", "caNamespace": "default"},
            ],
            "externalPeerOrganizations": [],
            "externalOrdererOrganizations": [],
            "ordererOrganizations": [{
                "mspID": "OrdererMSP", "caName": "ord-ca", "caNamespace": "default",
                "ordererEndpoints": ["ord-node1.default:7050"],   # interno
                "orderersToJoin": [],
                "externalOrderersToJoin": [{"host": "ord-node1.default", "port": 7053}],
            }],
            "orderers": [{"host": "ord-node1.default", "port": 7050, "tlsCert": tls}],
            "channelConfig": {
                "capabilities": ["V2_0"],
                "application": {"capabilities": ["V2_0"]},
                "orderer": {
                    "ordererType": "etcdraft",
                    "capabilities": ["V2_0"],
                    "batchTimeout": "2s",
                    "batchSize": {"maxMessageCount": 100, "absoluteMaxBytes": 1048576,
                                  "preferredMaxBytes": 524288},
                    "state": "STATE_NORMAL",
                    "etcdRaft": {"options": {"electionTick": 10, "heartbeatTick": 1,
                                             "maxInflightBlocks": 5,
                                             "snapshotIntervalSize": 16777216,
                                             "tickInterval": "500ms"}},
                },
            },
        },
    }

    def seguidor(org, msp):
        return {
            "apiVersion": "hlf.kungfusoftware.es/v1alpha1",
            "kind": "FabricFollowerChannel",
            "metadata": {"name": f"{CANAL}-{org}"},
            "spec": {
                "name": CANAL,
                "mspId": msp,
                "hlfIdentity": identidade(f"{org}-admin"),
                "anchorPeers": [{"host": f"{org}-peer0.default", "port": 7051}],  # interno
                "peersToJoin": [{"name": f"{org}-peer0", "namespace": "default"}],
                "externalPeersToJoin": [],
                "orderers": [{"url": "grpcs://ord-node1.default:7050", "certificate": tls}],
            },
        }

    os.makedirs(saida, exist_ok=True)
    yaml.safe_dump(principal, open(f"{saida}/canal.yaml", "w"), sort_keys=False)
    yaml.safe_dump_all([seguidor("org1", "Org1MSP"), seguidor("org2", "Org2MSP")],
                       open(f"{saida}/canal-seguidores.yaml", "w"), sort_keys=False)
    print(f"{saida}/canal.yaml e canal-seguidores.yaml gerados")


if __name__ == "__main__":
    if len(sys.argv) == 4 and sys.argv[1] == "no":
        no(sys.argv[2], sys.argv[3])
    elif len(sys.argv) == 3 and sys.argv[1] == "canal":
        canal(sys.argv[2])
    else:
        sys.exit(__doc__)
