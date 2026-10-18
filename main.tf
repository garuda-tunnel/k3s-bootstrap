# The only resource: one controller-side playbook run prepares this node.
# A failed run leaves the resource tainted, so the next apply re-runs the
# idempotent playbook. Host logic lives in ansible/k3s-node.yml, never here.
resource "terraform_data" "converge" {
  triggers_replace = [local.run_hash]

  lifecycle {
    # Inputs reach Ansible as extra vars, which it treats as trusted template
    # source: a value such as "{{ lookup('pipe', ...) }}" would run on the
    # controller. Reject Jinja delimiters in every input (nested ones included).
    precondition {
      condition     = !can(regex("\\{[{%#]", jsonencode(local.run_vars)))
      error_message = "No input may contain the Jinja delimiters {{, {% or {#: they would be evaluated as templates on the controller."
    }
  }

  provisioner "local-exec" {
    working_dir = path.module

    environment = {
      K3SB_VARS      = jsonencode(local.run_vars)
      ANSIBLE_CONFIG = "${abspath(path.module)}/ansible/ansible.cfg"
    }

    command = "uv run --no-project --with-requirements ansible/requirements.txt ansible-playbook -i localhost, ansible/k3s-node.yml -e \"$K3SB_VARS\""
  }
}
