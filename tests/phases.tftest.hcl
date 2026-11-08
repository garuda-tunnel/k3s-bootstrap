# Plan-time tests for the controller launch: what reaches the playbook and
# what re-runs it. Plan only, no providers (the local-exec never runs).
# The launch itself (the real local-exec command) is deliberately not tested
# here: the container tier (D11) and the VM tier (D12) apply the module end to
# end. Change sensitivity of run_hash is covered by tests/controller/test_run_hash.py.

variables {
  node_type            = "initial"
  host                 = "192.0.2.10"
  host_generation      = "projects/example/zones/example-b/instances/bootstrap-1"
  ssh_user             = "admin"
  ssh_private_key_file = "/secure/k3s-bootstrap.ed25519"
  authorized_keys      = ["ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIexamplekeymaterialnotarealkey admin@example"]
  k3s_version          = "v1.33.0+k3s1"
  cluster_cidrs        = ["10.42.0.0/16", "10.43.0.0/16"]
  sysctls              = { "net.ipv4.ip_forward" = "1" }
}

run "no_sensitive_variable" {
  command = plan

  # nonsensitive() fails on a value that is not sensitive, so every module
  # input must pass issensitive() == false (token-bearing inputs are removed).
  assert {
    condition = !anytrue([for value in [
      var.node_type, var.host, var.port, var.head, var.host_key_check, var.host_generation,
      var.bootstrap_user, var.ssh_user, var.ssh_private_key_file, var.authorized_keys,
      var.sysctls, var.kernel_modules, var.extra_packages, var.k3s_version, var.k3s_config, var.firewall_mode,
      var.api_trusted_cidrs, var.cluster_cidrs, var.grow_root_filesystem, var.apparmor_container_profile_fix,
      var.connection_timeout, var.readiness_timeout,
    ] : issensitive(value)])
    error_message = "no module input may be sensitive: join tokens are never caller input."
  }
}

run "run_vars_are_node_settings_only" {
  command = plan

  assert {
    condition = sort(keys(local.run_vars)) == sort([
      "node_type", "host", "port", "head", "host_key_check", "bootstrap_user", "ssh_user",
      "ssh_private_key_file", "authorized_keys", "sysctls", "kernel_modules", "extra_packages", "k3s_version",
      "k3s_config", "firewall_mode", "api_trusted_cidrs", "cluster_cidrs",
      "grow_root_filesystem", "apparmor_container_profile_fix",
      "connection_timeout", "readiness_timeout",
    ])
    error_message = "run_vars must hold exactly the documented node settings."
  }
  assert {
    condition     = !anytrue([for key in keys(local.run_vars) : can(regex("token|secret|password|passphrase", key))])
    error_message = "run_vars must not carry a token-like key."
  }
  assert {
    condition     = local.run_vars.sysctls["net.ipv4.ip_forward"] == "1"
    error_message = "run_vars must preserve caller sysctls."
  }
}

run "run_hash_covers_vars_generation_and_ansible_tree" {
  command = plan

  assert {
    condition     = length(terraform_data.converge.triggers_replace) == 1 && terraform_data.converge.triggers_replace[0] == local.run_hash
    error_message = "run_hash must be the only re-run trigger of the one controller playbook run."
  }
  assert {
    condition     = local.run_hash == sha256(jsonencode([local.run_vars, var.host_generation, local.ansible_tree_hash]))
    error_message = "run_hash must be sha256 over run_vars, host_generation and the ansible tree hash, and nothing else."
  }
  assert {
    condition     = length(fileset(path.module, "ansible/**")) > 0 && local.ansible_tree_hash != sha256("")
    error_message = "the ansible tree hash must cover the playbook files."
  }
}
