# -*- mode: ruby -*-
# Cluster Kubernetes de 3 nós para a PoC Hyperledger Fabric.
# `vagrant up` cria as VMs, prepara os nós, inicializa o cluster e
# faz o join dos workers (scripts 01, 02 e 03). O restante roda no
# control-plane (ver README).

NODES = [
  { name: "control-plane", ip: "192.168.56.10", mem: 4096, cpus: 2, role: "cp" },
  { name: "worker1",       ip: "192.168.56.11", mem: 7168, cpus: 2, role: "worker" },
  { name: "worker2",       ip: "192.168.56.12", mem: 7168, cpus: 2, role: "worker" },
]

Vagrant.configure("2") do |config|
  # Validado com Ubuntu 22.04 (jammy). Ver nota no README sobre o texto do TCC.
  config.vm.box = "ubuntu/jammy64"
  config.vm.box_check_update = false

  NODES.each do |n|
    config.vm.define n[:name] do |node|
      node.vm.hostname = n[:name]
      node.vm.network "private_network", ip: n[:ip]

      node.vm.provider "virtualbox" do |vb|
        vb.name   = "k8s-#{n[:name]}"
        vb.memory = n[:mem]
        vb.cpus   = n[:cpus]
        vb.linked_clone = true
        # Evita que o relógio da VM "salte" após pausas do host
        vb.customize ["guestproperty", "set", :id,
                      "/VirtualBox/GuestAdd/VBoxService/--timesync-set-threshold", 10000]
      end

      node.vm.provision "shell", path: "scripts/01-node-prep.sh", args: [n[:ip]]

      if n[:role] == "cp"
        node.vm.provision "shell", path: "scripts/02-cluster-init.sh"
      else
        node.vm.provision "shell", path: "scripts/03-join.sh"
      end
    end
  end
end
