# Module inputs. Host changes are performed exclusively by Ansible
# (see AGENTS.md); these variables only describe the target and the
# desired k3s setup.

variable "node_type" {
  description = "What this module call prepares: initial (first server, single-member embedded etcd), server (joins an existing cluster as a server) or agent. Servers must be applied one at a time: order them with depends_on in the caller."
  type        = string
  nullable    = false

  validation {
    condition     = contains(["initial", "server", "agent"], var.node_type)
    error_message = "node_type must be one of: initial, server, agent."
  }
}

variable "host" {
  description = "Hostname or IP address of the target host."
  type        = string

  validation {
    condition     = trimspace(var.host) != ""
    error_message = "host must not be blank."
  }
}

variable "port" {
  description = "SSH port of the target host."
  type        = number
  default     = 22

  validation {
    condition     = var.port >= 1 && var.port <= 65535 && floor(var.port) == var.port
    error_message = "port must be an integer in 1..65535."
  }
}

variable "head" {
  description = "SSH coordinates of a cluster member that is already a server, normally head = module.<initial>.head. Required for server and agent, forbidden for initial. The join address is head.host:6443. The module connects to it with ssh_private_key_file and host_key_check; user must be the head's admin user with passwordless sudo."
  type = object({
    host = string
    port = optional(number, 22)
    user = string
  })
  default = null

  validation {
    condition     = contains(["server", "agent"], var.node_type) ? var.head != null : var.head == null
    error_message = "head must be set when node_type is server or agent, and must be null when node_type is initial."
  }

  validation {
    condition     = var.head == null ? true : trimspace(var.head.host) != "" && trimspace(var.head.user) != ""
    error_message = "head.host and head.user must not be blank."
  }

  validation {
    condition     = var.head == null ? true : var.head.port >= 1 && var.head.port <= 65535 && floor(var.head.port) == var.head.port
    error_message = "head.port must be an integer in 1..65535."
  }
}

variable "host_key_check" {
  description = "Verify the SSH host keys of the target and the head against the caller's known_hosts (an unknown or changed key fails the run; ssh-keyscan once and persist it). false disables the check and trusts any key."
  type        = bool
  default     = true
  nullable    = false
}

variable "host_generation" {
  description = "Caller's stable machine identity of the host, for example an instance id. A recreated VM keeps its address, so changing this is what re-runs the playbook on it."
  type        = string
}

variable "bootstrap_user" {
  description = "SSH user available on the fresh host before ssh_user is provisioned."
  type        = string
  default     = "root"

  validation {
    condition     = trimspace(var.bootstrap_user) != ""
    error_message = "bootstrap_user must not be blank."
  }
}

variable "ssh_user" {
  description = "Administrative non-root SSH user provisioned on the host; it reaches root through sudo."
  type        = string

  validation {
    condition     = trimspace(var.ssh_user) != ""
    error_message = "ssh_user must not be blank."
  }

  validation {
    condition     = var.ssh_user != "root"
    error_message = "ssh_user must not be root: the module provisions a non-root admin user and reaches root through sudo."
  }
}

variable "ssh_private_key_file" {
  description = "Local path to the private key the controller uses for every SSH connection (target and head). The module never reads or copies its content; ssh reads it."
  type        = string
  nullable    = false

  validation {
    condition     = trimspace(var.ssh_private_key_file) != ""
    error_message = "ssh_private_key_file must not be blank."
  }
}

variable "authorized_keys" {
  description = "Public SSH keys authorized for ssh_user."
  type        = list(string)

  validation {
    condition     = length(var.authorized_keys) > 0
    error_message = "At least one public key must be provided in authorized_keys."
  }

  validation {
    condition     = alltrue([for key in var.authorized_keys : trimspace(key) != ""])
    error_message = "authorized_keys entries must not be blank."
  }
}

variable "sysctls" {
  description = "Additional sysctls to set on the host, for example { \"vm.max_map_count\" = \"262144\" }."
  type        = map(string)
  default     = {}
}

variable "kernel_modules" {
  description = "Kernel modules to load on boot."
  type        = list(string)
  default     = []
}

variable "k3s_version" {
  description = "Exact k3s version pin, for example v1.33.0+k3s1. Never a floating tag."
  type        = string

  validation {
    condition     = can(regex("^v\\d+\\.\\d+\\.\\d+\\+k3s\\d+$", var.k3s_version))
    error_message = "k3s_version must be an exact pin matching ^v\\d+\\.\\d+\\.\\d+\\+k3s\\d+$, for example v1.33.0+k3s1."
  }
}

variable "k3s_config" {
  description = "Extra k3s config file keys. Keys managed by the module are rejected. Put no secrets in it, whatever the option: it is passed on the controller command line and environment of the playbook run and hashed into the re-run trigger. Join tokens are never caller input."
  type        = any
  default     = {}

  validation {
    condition = can(keys(var.k3s_config)) && try(length(
      setintersection(
        # k3s also accepts the append form `key+`; compare the base name.
        [for key in keys(var.k3s_config) : trimsuffix(key, "+")],
        ["token", "token-file", "agent-token", "agent-token-file", "server", "cluster-init", "write-kubeconfig-mode", "write-kubeconfig-group", "data-dir", "datastore-endpoint", "cluster-reset", "cluster-reset-restore-path"]
      )
    ), 1) == 0
    error_message = "k3s_config must be an object or map and must not set token, token-file, agent-token, agent-token-file (also in the append form `key+`), server, cluster-init, write-kubeconfig-mode, write-kubeconfig-group, data-dir, datastore-endpoint, cluster-reset or cluster-reset-restore-path; the module manages them or they bypass its data safety."
  }
}

variable "firewall_mode" {
  description = "Firewall management: managed provisions rules for SSH and the k3s API, off leaves the host firewall untouched."
  type        = string
  default     = "managed"

  validation {
    condition     = contains(["managed", "off"], var.firewall_mode)
    error_message = "firewall_mode must be one of: managed, off."
  }
}

variable "api_trusted_cidrs" {
  description = "CIDRs allowed to reach the k3s API when firewall_mode is managed. The trusted set also always includes cluster_cidrs, and loopback is always allowed (the api_forward_target path). With an empty list the API is reachable only from those."
  type        = list(string)
  default     = []
  nullable    = false

  validation {
    condition     = alltrue([for cidr in var.api_trusted_cidrs : can(cidrhost(cidr, 0))])
    error_message = "Every api_trusted_cidrs entry must be a valid CIDR, for example 192.0.2.0/24."
  }
}

variable "cluster_cidrs" {
  description = "Pod and service network CIDRs the cluster uses (k3s defaults 10.42.0.0/16 and 10.43.0.0/16, or your cluster-cidr and service-cidr), the only cluster networks the managed firewall trusts. Required on initial and server nodes when firewall_mode is managed."
  type        = list(string)
  default     = []
  nullable    = false

  validation {
    condition     = alltrue([for cidr in var.cluster_cidrs : can(cidrhost(cidr, 0))])
    error_message = "Every cluster_cidrs entry must be a valid CIDR, for example 10.42.0.0/16."
  }

  validation {
    condition     = var.node_type == "agent" || var.firewall_mode != "managed" || length(var.cluster_cidrs) > 0
    error_message = "set the cluster's pod and service CIDRs in cluster_cidrs: the managed firewall on a server role trusts only those cluster networks."
  }

  validation {
    # k3s accepts a comma-separated string or a list; every CIDR must be
    # listed verbatim, or the managed firewall would drop cluster traffic.
    condition = var.node_type == "agent" || var.firewall_mode != "managed" || try(alltrue([
      for key in ["cluster-cidr", "service-cidr"] : try(var.k3s_config[key], null) == null ? true : alltrue([
        for cidr in(can(tolist(var.k3s_config[key])) ? tolist(var.k3s_config[key]) : split(",", var.k3s_config[key])) :
        contains(var.cluster_cidrs, trimspace(cidr))
      ])
    ]), false)
    error_message = "every CIDR in k3s_config cluster-cidr and service-cidr must also appear in cluster_cidrs (verbatim) when the managed firewall runs on a server role."
  }
}

variable "grow_root_filesystem" {
  description = "Grow the root partition and its ext4 or xfs filesystem to fill the disk. Preflight refuses any other root layout (LVM, a whole-disk filesystem, another filesystem type); set false there."
  type        = bool
  default     = true
}

variable "connection_timeout" {
  description = "Overall bound for each SSH connect and for the reconnect after a host-requested reboot, for example 5m; it must cover the host's shutdown and boot time."
  type        = string
  default     = "5m"

  validation {
    condition     = can(regex("^[1-9][0-9]*[smh]$", var.connection_timeout))
    error_message = "connection_timeout must match ^[1-9][0-9]*[smh]$, for example 30s, 5m or 1h."
  }
}

variable "readiness_timeout" {
  description = "Overall bound for waiting until the host is ready after bootstrap, for example 10m."
  type        = string
  default     = "10m"

  validation {
    condition     = can(regex("^[1-9][0-9]*[smh]$", var.readiness_timeout))
    error_message = "readiness_timeout must match ^[1-9][0-9]*[smh]$, for example 30s, 10m or 1h."
  }
}
