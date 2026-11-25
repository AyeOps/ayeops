# Ansible inventory

AyeOps does not run Ansible and does not need it. A team that already hardens or bootstraps its
hosts with its own playbooks can point them at the manifest instead of keeping a second list of
hosts: `aye inventory` writes an Ansible inventory from the manifest.

```sh
aye inventory examples/acme.yaml -o inventory.yaml
aye inventory examples/acme.yaml --format ansible-json -o inventory.json
aye inventory examples/acme.yaml -o inventory.yaml --check
```

Without `-o` the inventory goes to stdout. The command runs `aye check` first and writes nothing
when the manifest has an error.

## What it contains

Every host of the manifest is under `all.hosts` by its manifest name, with these variables:

| Variable | From the manifest |
|---|---|
| `ansible_host` | the host's `address`; without one Ansible connects to the host name, as `aye deploy` does |
| `ayeops_arch` | `arch` |
| `ayeops_zone` | `zone` |
| `ayeops_group` | `group`, else the host's `profile` (the same `group` label its metrics and logs carry) |
| `ayeops_project` | `project` |

A variable whose field the host does not have is left out, not written empty. The manifest has
no ssh settings per host, so the inventory sets no `ansible_port` or `ansible_user`: put them in
your `~/.ssh/config` or in `group_vars` next to the inventory.

The groups under `all.children`, each with its hosts in name order:

| Group | Its hosts |
|---|---|
| `zone_<zone>` | the hosts in that `zone` |
| `group_<group>` | the hosts whose `ayeops_group` is that group |
| `profile_<profile>` | the hosts that start from that [profile](manifest.md#profiles) |
| `module_<module>` | the hosts that enable the module; `false` turns it off and leaves the group |
| `service_<service>` | the hosts that run the service: the manifest `services` placed on the host (not `planned` or `disabled` ones) and the catalog services the host's modules render, such as `service_victoriametrics` or `service_node_exporter` |

Group names are lowercase, and every character outside `a-z`, `0-9` and `_` becomes `_`, so
`ai-usage-exporter` is `service_ai_usage_exporter` and a zone `DC East` is `zone_dc_east`. Two
values that sanitise to the same name share one group.

The top-level variable `ayeops_generated_by: aye inventory` marks the file as generated; the
YAML carries no comment, like everything else AyeOps writes. Keys are sorted, so the same
manifest gives the same bytes on every run. The JSON form is the same structure, which the YAML
inventory plugin of Ansible reads from a `.json` file as well.

The inventory holds names, addresses and the fields above only: no module settings, no
environment variable names and nothing from a `.env` file.

## In CI

A repository that commits the inventory checks it the way it checks a committed render:
`--check` writes nothing, prints a unified diff against the file and exits 1 when the file is
missing or differs, 0 when it matches. `--check` without `-o` is a usage error, exit 2.

## A playbook

```yaml
- hosts: service_postgres
  become: true
  tasks:
    - name: Keep the postgres data directory private
      ansible.builtin.file: {path: /var/lib/postgresql, mode: "0700"}
```

`ansible-playbook -i inventory.yaml harden.yaml` runs it on every host where the manifest places
a service named `postgres`.
