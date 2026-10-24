terraform {
  required_version = "= 1.16.5"
  required_providers {
    libvirt = {
      source  = "dmacvicar/libvirt"
      version = "= 0.9.9"
    }
  }
  backend "local" {}
}

provider "libvirt" {
  uri = "qemu:///system"
}

locals {
  name     = var.lab_name
  guest_ip = cidrhost(var.network_cidr, 10)
  mac      = var.mac_address
}

resource "libvirt_pool" "lab" {
  name = local.name
  type = "dir"
  create = {
    build     = true
    start     = true
    autostart = false
  }
  destroy = { delete = true }
  target = {
    path = "/var/lib/libvirt/images/${local.name}"
  }
}

resource "libvirt_network" "lab" {
  name      = local.name
  autostart = false
  forward   = { mode = "nat" }
  bridge    = { name = var.bridge_name }
  ips = [{
    address = cidrhost(var.network_cidr, 1)
    prefix  = tonumber(split("/", var.network_cidr)[1])
    dhcp = {
      ranges = [{ start = cidrhost(var.network_cidr, 10), end = cidrhost(var.network_cidr, 20) }]
      hosts  = [{ mac = local.mac, ip = local.guest_ip, name = local.name }]
    }
  }]
}

resource "libvirt_volume" "base" {
  name   = "ubuntu-noble-20260926.qcow2"
  pool   = libvirt_pool.lab.name
  target = { format = { type = "qcow2" } }
  create = { content = { url = var.verified_cloud_image } }
}

resource "libvirt_volume" "system" {
  name     = "system.qcow2"
  pool     = libvirt_pool.lab.name
  capacity = 40 * 1024 * 1024 * 1024
  target   = { format = { type = "qcow2" } }
  backing_store = {
    path   = libvirt_volume.base.path
    format = { type = "qcow2" }
  }
}

resource "libvirt_cloudinit_disk" "bootstrap" {
  name = local.name
  meta_data = yamlencode({
    instance-id    = local.name
    local-hostname = local.name
  })
  user_data = "#cloud-config\n${yamlencode({
    hostname         = local.name
    manage_etc_hosts = true
    ssh_pwauth       = false
    disable_root     = true
    package_update   = true
    packages         = ["qemu-guest-agent", "curl", "ca-certificates", "python3"]
    users = [{
      name                = "myscoutee"
      groups              = ["sudo"]
      sudo                = "ALL=(ALL) NOPASSWD:ALL"
      shell               = "/bin/bash"
      lock_passwd         = true
      ssh_authorized_keys = [trimspace(file(var.ssh_public_key_file))]
    }]
    write_files = [
      { path = "/etc/myscoutee-lab/versions.json", permissions = "0644", content = file("${path.module}/../versions.json") },
      { path = "/usr/local/sbin/myscoutee-install-k3s", permissions = "0755", content = file("${path.module}/../guest/install-k3s.sh") },
      { path = "/etc/systemd/system/k3s.service", permissions = "0644", content = file("${path.module}/../guest/k3s.service") },
      { path = "/etc/rancher/k3s/config.yaml", permissions = "0600", content = yamlencode({
        write-kubeconfig-mode = "0600"
        tls-san               = [local.guest_ip]
        disable               = ["traefik", "servicelb", "metrics-server"]
        secrets-encryption    = true
        node-name             = local.name
        kubelet-arg           = ["max-pods=40"]
      }) }
    ]
    runcmd = [
      ["systemctl", "enable", "--now", "qemu-guest-agent"],
      ["/usr/local/sbin/myscoutee-install-k3s"]
    ]
  })}"
}

resource "libvirt_volume" "bootstrap" {
  name   = "cloud-init.iso"
  pool   = libvirt_pool.lab.name
  create = { content = { url = libvirt_cloudinit_disk.bootstrap.path } }
}

resource "libvirt_domain" "lab" {
  name        = local.name
  type        = "kvm"
  memory      = var.memory_mib
  memory_unit = "MiB"
  vcpu        = 4
  # This is a single-host lab. Expose AVX required by the production Mongo image.
  cpu       = { mode = "host-passthrough" }
  autostart = false
  running   = var.running
  os = {
    type         = "hvm"
    type_arch    = "x86_64"
    type_machine = "q35"
  }
  devices = {
    channels = [{
      source = { unix = { mode = "bind" } }
      target = { virt_io = { name = "org.qemu.guest_agent.0" } }
    }]
    disks = [
      {
        driver = { type = "qcow2" }
        # Explicit file/chain paths let Ubuntu's per-domain AppArmor profile
        # authorize both images without changing global libvirt security policy.
        source = { file = { file = libvirt_volume.system.path } }
        backing_store = {
          format = { type = "qcow2" }
          source = { file = { file = libvirt_volume.base.path } }
        }
        target = { dev = "vda", bus = "virtio" }
      },
      {
        device    = "cdrom"
        driver    = { type = "raw" }
        read_only = true
        source    = { file = { file = libvirt_volume.bootstrap.path } }
        target    = { dev = "sda", bus = "sata" }
      }
    ]
    interfaces = [{
      model  = { type = "virtio" }
      mac    = { address = local.mac }
      source = { network = { network = libvirt_network.lab.name } }
    }]
  }
}

output "guest_ip" { value = local.guest_ip }
output "domain_id" { value = libvirt_domain.lab.id }
output "domain_uuid" { value = libvirt_domain.lab.uuid }
