locals {
  # Everything the playbook receives as extra vars. Node settings only: no
  # token, secret or key content (join data is gathered from the head inside
  # the playbook run).
  run_vars = {
    node_type            = var.node_type
    host                 = var.host
    port                 = var.port
    head                 = var.head
    host_key_check       = var.host_key_check
    bootstrap_user       = var.bootstrap_user
    ssh_user             = var.ssh_user
    ssh_private_key_file = var.ssh_private_key_file
    authorized_keys      = var.authorized_keys
    sysctls              = var.sysctls
    kernel_modules       = var.kernel_modules
    extra_packages       = var.extra_packages
    k3s_version          = var.k3s_version
    k3s_config           = var.k3s_config
    firewall_mode        = var.firewall_mode
    api_trusted_cidrs    = var.api_trusted_cidrs
    cluster_cidrs        = var.cluster_cidrs
    grow_root_filesystem = var.grow_root_filesystem
    connection_timeout   = var.connection_timeout
    readiness_timeout    = var.readiness_timeout
  }

  ansible_tree_hash = sha256(join("", [
    for filename in sort(tolist(fileset(path.module, "ansible/**"))) :
    "${filename}:${filesha256("${path.module}/${filename}")}"
  ]))

  # The only re-run trigger: the inputs, the machine identity and the playbook tree.
  run_hash = sha256(jsonencode([local.run_vars, var.host_generation, local.ansible_tree_hash]))
}
