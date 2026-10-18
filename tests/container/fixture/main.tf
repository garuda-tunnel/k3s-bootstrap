# Caller composition for tests/container/test_module_apply.py: one `initial`
# node in a container. The managed firewall is off (the container is not a
# supported firewall host) and the host key is not checked (an ephemeral
# container key).
terraform {
  required_version = ">= 1.9.0"
}

variable "port" {
  type = number
}

variable "host_generation" {
  type = string
}

variable "ssh_private_key_file" {
  type = string
}

variable "authorized_keys" {
  type = list(string)
}

module "node" {
  source = "../../.."

  node_type            = "initial"
  host                 = "127.0.0.1"
  port                 = var.port
  host_key_check       = false
  host_generation      = var.host_generation
  ssh_user             = "moduleapply"
  ssh_private_key_file = var.ssh_private_key_file
  authorized_keys      = var.authorized_keys
  firewall_mode        = "off"
  grow_root_filesystem = false # the container root is an overlay, not a partition
  k3s_version          = "v1.33.0+k3s1"
}
