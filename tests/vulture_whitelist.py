"""Names that pytest consumes by convention and vulture's static scan cannot see used."""

pytestmark  # unused variable: module-level marker applied by pytest
restore_sshd_state  # unused function: fixture requested through usefixtures
fixture_work  # unused function: pytest fixture registered under the name "work"
fixture_module  # unused function: pytest fixture registered under the name "module"
fixture_controller_key  # unused function: pytest fixture registered under the name "controller_key"
fixture_child  # unused function: pytest fixture registered under the name "child"
fixture_apply_host  # unused function: pytest fixture registered under the name "apply_host"
