# One node per state: the VM itself is created by Vagrant
# (tests/vm/vagrant/Vagrantfile) before tofu runs, so no provider is needed.
# The VMs reach each other and the runner directly on the vagrant-libvirt
# management network. Host keys are not checked: every stand is a new VM.

terraform {
  required_version = ">= 1.9.0"
}

variable "name_suffix" {
  type = string
}

variable "host" {
  description = "IPv4 of the stand VM, from its libvirt DHCP lease."
  type        = string
}

variable "host_generation" {
  description = "Stable machine identity of the VM (its libvirt domain name)."
  type        = string
}

variable "node_type" {
  type    = string
  default = "initial"
}

variable "head" {
  type = object({
    host = string
    user = string
  })
  default = null
}

variable "k3s_version" {
  type = string
}

variable "readiness_timeout" {
  type    = string
  default = "5m"
}

variable "ssh_private_key_file" {
  type = string
}

variable "authorized_keys" {
  type = list(string)
}

variable "firewall_mode" {
  type    = string
  default = "managed"
}

variable "api_trusted_cidrs" {
  type    = list(string)
  default = []
}

module "k3s_bootstrap" {
  source = "../../.."

  node_type            = var.node_type
  head                 = var.head
  host                 = var.host
  host_key_check       = false
  host_generation      = var.host_generation
  bootstrap_user       = "vagrant"
  ssh_user             = "ops"
  ssh_private_key_file = var.ssh_private_key_file
  authorized_keys      = var.authorized_keys
  k3s_version          = var.k3s_version
  readiness_timeout    = var.readiness_timeout
  firewall_mode        = var.firewall_mode
  api_trusted_cidrs    = var.api_trusted_cidrs
  cluster_cidrs        = ["10.42.0.0/16", "10.43.0.0/16"]
}

output "host" {
  value = var.host
}

output "instance_name" {
  value = var.host_generation
}
