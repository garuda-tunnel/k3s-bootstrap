# Caller composition for test_cluster.py: one `initial`
# hub and two agents applied in parallel through `head = module.hub.head`.
# The VMs exist before tofu runs (Vagrant, one per stand).

terraform {
  required_version = ">= 1.9.0"
}

variable "hub" {
  type = object({ host = string, host_generation = string })
}

variable "agents" {
  type = map(object({ host = string, host_generation = string }))
}

variable "k3s_version" {
  type = string
}

variable "ssh_private_key_file" {
  type = string
}

variable "authorized_keys" {
  type = list(string)
}

variable "api_trusted_cidrs" {
  type = list(string)
}

locals {
  common = {
    bootstrap_user       = "vagrant"
    ssh_user             = "ops"
    ssh_private_key_file = var.ssh_private_key_file
    authorized_keys      = var.authorized_keys
    k3s_version          = var.k3s_version
  }
}

module "hub" {
  source = "../../../.."

  node_type            = "initial"
  host                 = var.hub.host
  host_generation      = var.hub.host_generation
  host_key_check       = false
  bootstrap_user       = local.common.bootstrap_user
  ssh_user             = local.common.ssh_user
  ssh_private_key_file = local.common.ssh_private_key_file
  authorized_keys      = local.common.authorized_keys
  k3s_version          = local.common.k3s_version
  api_trusted_cidrs    = var.api_trusted_cidrs
  cluster_cidrs        = ["10.42.0.0/16", "10.43.0.0/16"]
}

module "agent" {
  source   = "../../../.."
  for_each = var.agents

  node_type            = "agent"
  head                 = module.hub.head
  host                 = each.value.host
  host_generation      = each.value.host_generation
  host_key_check       = false
  bootstrap_user       = local.common.bootstrap_user
  ssh_user             = local.common.ssh_user
  ssh_private_key_file = local.common.ssh_private_key_file
  authorized_keys      = local.common.authorized_keys
  k3s_version          = local.common.k3s_version
  firewall_mode        = "off"
}
