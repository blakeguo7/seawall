# The Docker sandbox

By default the shell commands an agent runs are your own processes: they can read what you can read and delete what you can delete. The permission layers (approvals, risk labels, the credential-path rules) are a tripwire that catches known-dangerous shapes. They do not stop a command they fail to recognise. The Docker sandbox is the other kind of protection: the commands run in a container that is closed down, so what they can reach is limited whatever they are.

It is off by default. To turn it on:

```json
{
  "sandbox": {
    "enabled": true,
    "backend": "docker",
    "fail_if_unavailable": true
  }
}
```

With `fail_if_unavailable` off, a session that cannot reach Docker (not installed, not running) carries on **without** a sandbox and logs a warning. Turn it on if the sandbox is the point. A missing image that may not be built (`auto_build_image` off) is an error either way: the session does not start.

The image `seawall-sandbox:latest` is built on first use from a small Dockerfile (Python 3.11, bash, git, ripgrep, a non-root user). Building it pulls the base image and some packages from the internet, so the first start needs a connection and some time. Set `sandbox.docker.auto_build_image` to `false` to build or supply the image yourself.

## What the container is started with

Each session starts one container, `seawall-sandbox-<session id>`, and runs the shell commands, `grep` and `glob` inside it with `docker exec`.

| What | Default | Setting (`sandbox.docker.`) |
| --- | --- | --- |
| No network at all | always | none: there is no switch |
| 2 CPUs | `2.0` | `cpu_limit` (0 for no limit) |
| 4 GB of memory, and no swap, so a runaway command is killed instead of slowing the machine | `"4g"` | `memory_limit` (`""` for no limit) |
| At most 512 processes, which stops a fork bomb | `512` | `pids_limit` (0 for no limit) |
| Every Linux capability removed | on | `cap_drop_all` |
| No gaining privileges through setuid programs | on | `no_new_privileges` |
| Root filesystem read-only; `/tmp` is a 512 MB writable memory disk; `HOME` is `/tmp` | on | `read_only_root`, `tmp_size` |
| A proper PID 1 that reaps the processes commands leave behind | always | none |
| Labels `seawall.sandbox=1` and `seawall.session=<id>`, so a container can be found again (`docker ps --filter label=seawall.sandbox=1`) | always | none |

`/tmp` is not mounted `noexec`, because build and test tools run programs from it. A tool that needs somewhere else to write can be given a mount in `sandbox.docker.extra_mounts`.

The host's environment variables do not follow a command into the container. Hooks, background tasks and cron jobs build the environment of a command from the host's and add a few variables of their own; only the added ones (and any that differ from the host's) are passed on, so API keys and tokens in your shell stay out and the container keeps its own `PATH` and `HOME`. The one case this gets wrong is a task that sets a variable to the value the host already has: the container then does not see it.

## What it does not do

- **Your project directory is mounted read-write**, at the same path. The agent can edit and delete your real files there. That is what a coding assistant is for, but it means the sandbox protects everything *around* the project, not the project.
- **File tools run on the host.** `read_file`, `write_file` and `edit_file` do not go through the container; while the sandbox is active they refuse paths outside the project directory. `notebook_edit` has no such check.
- **One container per process.** The HTTP service refuses to start with the Docker sandbox enabled, and two sessions in one process would share it.
- **The network is all or nothing.** `allowed_domains` and `denied_domains` apply to the `srt` backend only; the Docker backend ignores them, logs a warning and stays offline. A task that needs `pip install` cannot have it.
- **It trusts the image.** The image is built from a Dockerfile in the package, or is the one you name.

## Checking it

Unit tests cover the command the container is started with and the environment filter. They cannot tell you that Docker accepted it, so the first time (and after changing the settings) it is worth starting a container and trying to break out of it: write to `/etc`, open a connection, start a hundred processes, and look at `env`. `tests/test_sandbox/` has the checks that do not need Docker.
