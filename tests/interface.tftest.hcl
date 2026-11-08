# Plan-time interface tests: input validation and outputs.
# Plan only, no providers. Every run starts from the file-level valid
# base configuration (an initial node) and overrides only what it names.

variables {
  node_type            = "initial"
  host                 = "192.0.2.10"
  host_generation      = "projects/example/zones/example-b/instances/bootstrap-1"
  ssh_user             = "admin"
  ssh_private_key_file = "/secure/k3s-bootstrap.ed25519"
  authorized_keys      = ["ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIexamplekeymaterialnotarealkey admin@example"]
  k3s_version          = "v1.33.0+k3s1"
  cluster_cidrs        = ["10.42.0.0/16", "10.43.0.0/16"]
}

# --- base --------------------------------------------------------------

run "accepts_valid_initial" {
  command = plan

  assert {
    condition     = output.api_forward_target == "127.0.0.1:6443"
    error_message = "initial must forward the API to 127.0.0.1:6443."
  }

  assert {
    condition     = local.run_vars.host_key_check == true && local.run_vars.head == null
    error_message = "host_key_check must default to true and an initial node has no head."
  }
}

run "accepts_server_with_head" {
  command = plan

  variables {
    node_type = "server"
    head      = { host = "192.0.2.1", user = "admin" }
  }

  assert {
    condition     = local.run_vars.head.port == 22
    error_message = "head.port must default to 22."
  }
}

run "accepts_agent_with_head_and_open_host_keys" {
  command = plan

  variables {
    node_type      = "agent"
    head           = { host = "192.0.2.1", user = "admin" }
    host_key_check = false
  }

  assert {
    condition     = local.run_vars.host_key_check == false
    error_message = "host_key_check must reach the playbook as a bool."
  }
}

# --- node_type and head ------------------------------------------------

run "rejects_node_type_bogus" {
  command = plan

  variables {
    node_type = "bogus"
  }

  expect_failures = [var.node_type]
}

run "rejects_server_without_head" {
  command = plan

  variables {
    node_type = "server"
  }

  expect_failures = [var.head]
}

run "rejects_initial_with_head" {
  command = plan

  variables {
    head = { host = "192.0.2.1", user = "admin" }
  }

  expect_failures = [var.head]
}

run "rejects_blank_head_host" {
  command = plan

  variables {
    node_type = "agent"
    head      = { host = " ", user = "admin" }
  }

  expect_failures = [var.head]
}

run "rejects_blank_head_user" {
  command = plan

  variables {
    node_type = "agent"
    head      = { host = "192.0.2.1", user = "" }
  }

  expect_failures = [var.head]
}

run "rejects_head_port_above_range" {
  command = plan

  variables {
    node_type = "agent"
    head      = { host = "192.0.2.1", port = 65536, user = "admin" }
  }

  expect_failures = [var.head]
}

run "rejects_root_ssh_user" {
  command = plan

  variables {
    ssh_user = "root"
  }

  expect_failures = [var.ssh_user]
}

# --- k3s_version must be an exact pin ----------------------------------

run "rejects_unpinned_k3s_version_latest" {
  command = plan

  variables {
    k3s_version = "latest"
  }

  expect_failures = [var.k3s_version]
}

run "rejects_unpinned_k3s_version_null" {
  command = plan

  variables {
    k3s_version = null
  }

  expect_failures = [var.k3s_version]
}

# --- k3s_config --------------------------------------------------------

run "accepts_mixed_k3s_config_values" {
  command = plan

  variables {
    k3s_config = { "node-name" = "bootstrap-1", "tls-san" = ["192.0.2.10"] }
  }

  assert {
    condition     = local.run_vars.k3s_config["tls-san"] == ["192.0.2.10"]
    error_message = "mixed k3s_config values must reach the playbook vars unchanged."
  }
}

run "rejects_reserved_k3s_config_key_data-dir" {
  command = plan

  variables {
    k3s_config = { "data-dir" = "x" }
  }

  expect_failures = [var.k3s_config]
}

run "rejects_reserved_k3s_config_key_token" {
  command = plan

  variables {
    k3s_config = { "token" = "x" }
  }

  expect_failures = [var.k3s_config]
}

run "rejects_reserved_k3s_config_key_cluster-reset" {
  command = plan

  variables {
    k3s_config = { "cluster-reset" = "x" }
  }

  expect_failures = [var.k3s_config]
}

# --- blank and range checks --------------------------------------------

run "rejects_empty_authorized_keys" {
  command = plan

  variables {
    authorized_keys = []
  }

  expect_failures = [var.authorized_keys]
}

run "rejects_blank_authorized_key" {
  command = plan

  variables {
    authorized_keys = ["ssh-ed25519 AAAA admin@example", " "]
  }

  expect_failures = [var.authorized_keys]
}

run "rejects_blank_host" {
  command = plan

  variables {
    host = " "
  }

  expect_failures = [var.host]
}

run "rejects_blank_ssh_user" {
  command = plan

  variables {
    ssh_user = " "
  }

  expect_failures = [var.ssh_user]
}

run "rejects_blank_bootstrap_user" {
  command = plan

  variables {
    bootstrap_user = " "
  }

  expect_failures = [var.bootstrap_user]
}

run "rejects_port_zero" {
  command = plan

  variables {
    port = 0
  }

  expect_failures = [var.port]
}

run "rejects_port_above_range" {
  command = plan

  variables {
    port = 65536
  }

  expect_failures = [var.port]
}

run "rejects_unparsable_readiness_timeout" {
  command = plan

  variables {
    readiness_timeout = "10 minutes"
  }

  expect_failures = [var.readiness_timeout]
}

run "rejects_unparsable_connection_timeout" {
  command = plan

  variables {
    connection_timeout = "5 minutes"
  }

  expect_failures = [var.connection_timeout]
}

# --- extra_packages ------------------------------------------------------

run "rejects_invalid_extra_package_name" {
  command = plan

  variables {
    extra_packages = ["jq", "foo bar"]
  }

  expect_failures = [var.extra_packages]
}

run "accepts_default_extra_packages_as_empty_list" {
  command = plan

  assert {
    condition     = jsonencode(local.run_vars.extra_packages) == "[]"
    error_message = "extra_packages must default to an empty list that reaches the playbook vars."
  }
}

run "passes_extra_packages_to_playbook" {
  command = plan

  variables {
    extra_packages = ["jq", "qemu-guest-agent", "libstdc++6", "python3.14"]
  }

  assert {
    condition     = local.run_vars.extra_packages == tolist(var.extra_packages)
    error_message = "Valid package names must reach the playbook unchanged."
  }
}

# --- apparmor_container_profile_fix --------------------------------------

run "accepts_default_apparmor_fix_as_true" {
  command = plan

  assert {
    condition     = local.run_vars.apparmor_container_profile_fix == true
    error_message = "apparmor_container_profile_fix must default to true and reach the playbook vars."
  }
}

run "passes_apparmor_fix_false_to_playbook" {
  command = plan

  variables {
    apparmor_container_profile_fix = false
  }

  assert {
    condition     = local.run_vars.apparmor_container_profile_fix == false
    error_message = "an explicit apparmor_container_profile_fix = false must reach the playbook unchanged."
  }
}

# --- firewall and CIDRs ------------------------------------------------

run "rejects_unknown_firewall_mode" {
  command = plan

  variables {
    firewall_mode = "iptables"
  }

  expect_failures = [var.firewall_mode]
}

run "rejects_invalid_api_trusted_cidr" {
  command = plan

  variables {
    api_trusted_cidrs = ["192.0.2.0/24", "not-a-cidr"]
  }

  expect_failures = [var.api_trusted_cidrs]
}

run "rejects_invalid_cluster_cidr" {
  command = plan

  variables {
    cluster_cidrs = ["10.42.0.0/33"]
  }

  expect_failures = [var.cluster_cidrs]
}

run "rejects_initial_managed_without_cluster_cidrs" {
  command = plan

  variables {
    cluster_cidrs = []
  }

  expect_failures = [var.cluster_cidrs]
}

run "accepts_empty_cluster_cidrs_when_firewall_off" {
  command = plan

  variables {
    firewall_mode = "off"
    cluster_cidrs = []
  }
}

run "accepts_empty_cluster_cidrs_on_agent" {
  command = plan

  variables {
    node_type     = "agent"
    head          = { host = "192.0.2.1", user = "admin" }
    cluster_cidrs = []
  }
}

run "accepts_mixed_ipv4_ipv6_cidr_lists" {
  command = plan

  variables {
    api_trusted_cidrs = ["192.0.2.0/24", "2001:db8::/32"]
    cluster_cidrs     = ["10.42.0.0/16", "fd00:42::/48"]
  }

  assert {
    condition     = length(local.run_vars.api_trusted_cidrs) == 2 && length(local.run_vars.cluster_cidrs) == 2
    error_message = "both CIDR lists must reach the playbook vars."
  }
}

run "accepts_custom_cidrs_listed_in_cluster_cidrs" {
  command = plan

  variables {
    cluster_cidrs = ["10.44.0.0/16", "10.45.0.0/16"]
    k3s_config    = { "cluster-cidr" = "10.44.0.0/16", "service-cidr" = "10.45.0.0/16" }
  }
}

run "accepts_dual_stack_cidr_string_and_list" {
  command = plan

  variables {
    cluster_cidrs = ["10.44.0.0/16", "fd00:44::/56", "10.45.0.0/16"]
    k3s_config    = { "cluster-cidr" = "10.44.0.0/16,fd00:44::/56", "service-cidr" = ["10.45.0.0/16"] }
  }
}

run "accepts_unlisted_custom_cidr_when_firewall_off" {
  command = plan

  variables {
    firewall_mode = "off"
    k3s_config    = { "cluster-cidr" = "10.44.0.0/16" }
  }
}

run "accepts_unlisted_custom_cidr_on_agent" {
  command = plan

  variables {
    node_type  = "agent"
    head       = { host = "192.0.2.1", user = "admin" }
    k3s_config = { "cluster-cidr" = "10.44.0.0/16" }
  }
}

run "rejects_cluster_cidr_missing_from_cluster_cidrs" {
  command = plan

  variables {
    k3s_config = { "cluster-cidr" = "10.44.0.0/16" }
  }

  expect_failures = [var.cluster_cidrs]
}

run "rejects_service_cidr_missing_from_cluster_cidrs" {
  command = plan

  variables {
    k3s_config = { "service-cidr" = ["10.45.0.0/16"] }
  }

  expect_failures = [var.cluster_cidrs]
}

run "rejects_dual_stack_cidr_with_one_unlisted" {
  command = plan

  variables {
    cluster_cidrs = ["10.44.0.0/16"]
    k3s_config    = { "cluster-cidr" = "10.44.0.0/16,fd00:44::/56" }
  }

  expect_failures = [var.cluster_cidrs]
}

# --- appended reserved keys, key file, templated inputs ----------------

run "rejects_reserved_k3s_config_key_token_append" {
  command = plan

  variables {
    k3s_config = { "token+" = "x" }
  }

  expect_failures = [var.k3s_config]
}

run "rejects_reserved_k3s_config_key_token-file" {
  command = plan

  variables {
    k3s_config = { "token-file" = "x" }
  }

  expect_failures = [var.k3s_config]
}

run "rejects_reserved_k3s_config_key_agent-token" {
  command = plan

  variables {
    k3s_config = { "agent-token" = "x" }
  }

  expect_failures = [var.k3s_config]
}

run "rejects_reserved_k3s_config_key_agent-token-file" {
  command = plan

  variables {
    k3s_config = { "agent-token-file" = "x" }
  }

  expect_failures = [var.k3s_config]
}

run "rejects_reserved_k3s_config_key_server" {
  command = plan

  variables {
    k3s_config = { "server" = "x" }
  }

  expect_failures = [var.k3s_config]
}

run "rejects_reserved_k3s_config_key_cluster-init" {
  command = plan

  variables {
    k3s_config = { "cluster-init" = "x" }
  }

  expect_failures = [var.k3s_config]
}

run "rejects_reserved_k3s_config_key_write-kubeconfig-mode" {
  command = plan

  variables {
    k3s_config = { "write-kubeconfig-mode" = "x" }
  }

  expect_failures = [var.k3s_config]
}

run "rejects_reserved_k3s_config_key_write-kubeconfig-group" {
  command = plan

  variables {
    k3s_config = { "write-kubeconfig-group" = "x" }
  }

  expect_failures = [var.k3s_config]
}

run "rejects_reserved_k3s_config_key_datastore-endpoint" {
  command = plan

  variables {
    k3s_config = { "datastore-endpoint" = "x" }
  }

  expect_failures = [var.k3s_config]
}

run "rejects_reserved_k3s_config_key_cluster-reset-restore-path" {
  command = plan

  variables {
    k3s_config = { "cluster-reset-restore-path" = "x" }
  }

  expect_failures = [var.k3s_config]
}

run "rejects_reserved_k3s_config_key_agent-token_append" {
  command = plan

  variables {
    k3s_config = { "agent-token+" = "x" }
  }

  expect_failures = [var.k3s_config]
}

run "rejects_whitespace_ssh_private_key_file" {
  command = plan

  variables {
    ssh_private_key_file = "  "
  }

  expect_failures = [var.ssh_private_key_file]
}

run "rejects_templated_host" {
  command = plan

  variables {
    host = "{{ lookup('pipe', 'id') }}"
  }

  expect_failures = [terraform_data.converge]
}

run "rejects_templated_head_user" {
  command = plan

  variables {
    node_type = "agent"
    head      = { host = "192.0.2.1", user = "{% if true %}admin{% endif %}" }
  }

  expect_failures = [terraform_data.converge]
}

run "rejects_comment_delimiter_in_authorized_key" {
  command = plan

  variables {
    authorized_keys = ["ssh-ed25519 AAAA {# x #}"]
  }

  expect_failures = [terraform_data.converge]
}

# --- outputs -----------------------------------------------------------

run "initial_outputs_head_and_api" {
  command = plan

  variables {
    port = 2222
  }

  assert {
    condition     = output.head == { host = "192.0.2.10", port = 2222, user = "admin" }
    error_message = "head must carry the node SSH coordinates and ssh_user."
  }

  assert {
    condition     = output.kubeconfig_path == "/etc/rancher/k3s/k3s.yaml"
    error_message = "initial must expose the kubeconfig path."
  }
}

run "server_outputs_head" {
  command = plan

  variables {
    node_type = "server"
    head      = { host = "192.0.2.1", user = "admin" }
  }

  assert {
    condition     = output.head != null && output.api_forward_target == "127.0.0.1:6443"
    error_message = "a server must expose head and the API forward target."
  }
}

run "agent_outputs_are_null" {
  command = plan

  variables {
    node_type = "agent"
    head      = { host = "192.0.2.1", user = "admin" }
  }

  assert {
    condition     = output.head == null
    error_message = "an agent is never a head; head must be null."
  }

  assert {
    condition     = output.kubeconfig_path == null
    error_message = "agents hold no kubeconfig; kubeconfig_path must be null."
  }

  assert {
    condition     = output.api_forward_target == null
    error_message = "agents do not serve the API; api_forward_target must be null."
  }
}
