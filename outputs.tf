# All outputs wait for the converge run: a caller that consumes them (a
# joining node via head, or the API forward) only proceeds once this node is
# ready. They are null on agents, which are never a head.

output "head" {
  description = "SSH coordinates of this node as a head for server and agent modules (head = module.<name>.head). Null for agent nodes."
  value       = var.node_type == "agent" ? null : { host = var.host, port = var.port, user = var.ssh_user }
  depends_on  = [terraform_data.converge]
}

output "kubeconfig_path" {
  description = "Path to the admin kubeconfig on the host. Null for agent nodes."
  value       = var.node_type == "agent" ? null : "/etc/rancher/k3s/k3s.yaml"
  depends_on  = [terraform_data.converge]
}

output "api_forward_target" {
  description = "Local address the k3s API is reachable on. Null for agent nodes."
  value       = var.node_type == "agent" ? null : "127.0.0.1:6443"
  depends_on  = [terraform_data.converge]
}
