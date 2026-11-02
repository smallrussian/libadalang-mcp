# Running lal-mcp as a service in your RHEL UBI 10 image

lal-mcp runs as a systemd service in HTTP mode. It loads the project once, keeps it warm,
and Codex or Claude connect to it by URL with a bearer token. There's one service
instance per repository: `lal-mcp@<name>`, configured by `/etc/lal-mcp/<name>.env`.

You add it to the image you already have, with one `RUN` that clones this repository and
installs the server as a service. It uses the GNAT Pro and Libadalang already in the
image, and adds only the server: a venv with the pinned `mcp`, the unit, and a system
user.

Files (in `deploy/`):

| file | what it is |
| --- | --- |
| `install.sh` | installs the server, the venv, the `lal-mcp` user and the unit, and with `--enable` sets up an instance. Run it as root, in your image's build or on a host |
| `lal-mcp@.service` | the systemd unit template |
| `lal-mcp.env.example` | the settings for one instance |
| `lal-mcp-serve` | the launcher the unit runs; reads the settings, starts the server |

## What your image needs

- **GNAT Pro and Libadalang, x86_64-linux, same release** (tested with 26.1). The
  `x86-linux` packages are 32-bit and don't work. `install.sh` looks for:
  - `gnat` in `<gnatpro-prefix>/bin`;
  - the Libadalang wheel in `<libadalang-prefix>/share/libadalang/python/`.

  Both prefixes default to `/opt/gnatpro/26.1`.
- **systemd as init** (`ubi10/ubi-init`, or your own image running `/sbin/init`) to run it
  as a service. Without systemd, see [Without systemd](#without-systemd).
- The `RUN` below installs the rest from UBI's repositories: `git-core`, `python3` (3.12
  on UBI 10) with `python3-pip`, `util-linux`, `shadow-utils`.

## 1. Add it to your Containerfile

One step:

```dockerfile
# lal-mcp: Libadalang MCP server, run by systemd as lal-mcp@default
RUN dnf -y install --setopt=install_weak_deps=False git-core python3 python3-pip util-linux shadow-utils \
    && dnf clean all \
    && git clone --depth 1 https://github.com/smallrussian/libadalang-mcp.git /tmp/lal-mcp \
    && /tmp/lal-mcp/deploy/install.sh --gnatpro-prefix /opt/gnatpro/26.1 --enable default \
    && rm -rf /tmp/lal-mcp /root/.cache
EXPOSE 8765
```

The `RUN` does the following:

- **Clones** the repository, and deletes the clone at the end of the same `RUN`, so it
  doesn't stay in the image.
- **Installs** the server into `/opt/lal-mcp`, with a venv holding the pinned `mcp` and
  the Libadalang wheel from your GNAT Pro install. It also creates the system user
  `lal-mcp` (uid 990) and installs the unit `lal-mcp@.service`.
- **Self-checks:** it imports Libadalang and `mcp` as `lal-mcp`, so a wrong prefix fails
  the build, not the container.
- **`--enable default`:**
  - writes `/etc/lal-mcp/default.env`: repository at `/ws`, listening on all interfaces,
    token from the podman secret `/run/secrets/lal-mcp-token`;
  - runs `systemctl enable lal-mcp@default`, so the service starts with the container;
  - at run time, mount your own env file over `default.env` to change anything.

Adapt it:

- **GNAT Pro location:** set `--gnatpro-prefix` to where your image has GNAT Pro. If
  Libadalang is in a different prefix, add `--libadalang-prefix <dir>`.
- **A fixed version:** clone a tag or commit, not the default branch:
  `git clone --depth 1 --branch <tag> ...`.
- **Air-gapped build:** clone from your internal mirror, and add
  `--wheelhouse <dir>` (see [Air-gapped builds](#air-gapped-builds)).
- **Size:** the step adds about 300 MB. Most of it is the venv, because the Libadalang
  wheel bundles its native libraries.
- **No GNAT Pro in the image yet:** `install.sh --vendor <dir>` installs it and
  Libadalang from the AdaCore zips in `<dir>`. AdaCore's `doinstall` needs `make`,
  `ncurses` (for `clear`), `tar` and `gzip`.

## 2. Create the token

`run_lal` executes Python in the server, so anyone who can reach the port can run code in
the container. The default instance listens on all interfaces, so it always requires
a token. Store it as a podman secret:

```bash
openssl rand -hex 32 > lal-mcp-token     # keep a copy: clients need it
podman secret create lal-mcp-token lal-mcp-token
```

## 3. Run

Mount the repository at `/ws`, read-only:

```bash
podman run -d --name mission-ada \
  -v /srv/repos/mission:/ws:ro,Z \
  -p 127.0.0.1:8765:8765 \
  --secret lal-mcp-token,type=mount,uid=990,gid=990,mode=0400 \
  <your-image>
```

- `:Z` relabels the folder for SELinux. Without it, the service gets "permission denied"
  reading the sources on RHEL.
- `-p 127.0.0.1:8765:8765` publishes the port to the host only. Use `-p 8765:8765` to
  accept connections from other machines.
- `uid=990` is the `lal-mcp` user (`install.sh --uid` changes it). The service reads the
  token from `/run/secrets/lal-mcp-token`.
- One repository per container: give each its own name and host port
  (`-p 127.0.0.1:8766:8765`). Or run several instances in one container: see
  [Several repositories in one container](#several-repositories-in-one-container).

The service runs in the repository root, so it uses the repository's `.lal-mcp.toml`. If the
repository has none, or its paths are for another machine (Windows drive letters, a
different `BASE_DIR`), write the settings in an env file and mount it over the default
one:

```bash
cat > mission.env <<'EOF'
LAL_MCP_WORKSPACE=/ws
LAL_MCP_ARGS=-P /ws/Build/Projects/Master_Dev.gpr -XBASE_DIR=/ws -XCOVERAGE_TYPE=NONE
LAL_MCP_HOST=0.0.0.0
LAL_MCP_PORT=8765
LAL_MCP_TOKEN_FILE=/run/secrets/lal-mcp-token
GNATPRO_PREFIX=/opt/gnatpro/26.1
EOF
podman run -d --name mission-ada \
  -v /srv/repos/mission:/ws:ro,Z \
  -v ./mission.env:/etc/lal-mcp/default.env:ro,Z \
  -p 127.0.0.1:8765:8765 \
  --secret lal-mcp-token,type=mount,uid=990,gid=990,mode=0400 \
  <your-image>
```

All the settings are in [Settings](#settings).

## 4. Check it

```bash
curl -s http://127.0.0.1:8765/healthz                       # {"status": "ok", "project_loaded": true}
podman exec mission-ada systemctl status lal-mcp@default
podman exec mission-ada journalctl -u lal-mcp@default -f    # the server log
```

`/healthz` needs no token. It returns 200 once the project is loaded and 503 if it isn't
(for example, Libadalang failed to import). The port opens only after the project has
loaded: for a large project that takes a minute or more after the container starts.

For `podman healthcheck` and the health column in `podman ps`, add this to
`podman run`:

```bash
  --health-cmd "/opt/lal-mcp/venv/bin/python -c \"import urllib.request; urllib.request.urlopen('http://127.0.0.1:8765/healthz', timeout=4)\"" \
  --health-start-period 10m
```

## 5. Start it with the host

Make it a Quadlet, so the host's systemd starts the container at boot. As root, create
`/etc/containers/systemd/mission-ada.container`:

```ini
[Unit]
Description=Ada tools for the mission repository

[Container]
Image=<your-image>
ContainerName=mission-ada
Volume=/srv/repos/mission:/ws:ro,Z
PublishPort=127.0.0.1:8765:8765
Secret=lal-mcp-token,type=mount,uid=990,gid=990,mode=0400
# Volume=/etc/lal-mcp/mission.env:/etc/lal-mcp/default.env:ro,Z

[Service]
Restart=always

[Install]
WantedBy=multi-user.target
```

Then:

```bash
systemctl daemon-reload
systemctl start mission-ada
```

## Several repositories in one container

Each instance needs its own env file, workspace and port. For instance `fleet`:

```bash
podman exec mission-ada sh -c 'cp /etc/lal-mcp/default.env /etc/lal-mcp/fleet.env'
```

1. Edit it: `LAL_MCP_WORKSPACE=/ws-fleet`, `LAL_MCP_PORT=8766`.
2. Mount the repository there (`-v /srv/repos/fleet:/ws-fleet:ro,Z`), and publish the
   port.
3. Start it: `podman exec mission-ada systemctl enable --now lal-mcp@fleet`.

Changes made with `podman exec` are lost when the container is recreated. To keep them,
mount the env files instead (`-v ./fleet.env:/etc/lal-mcp/fleet.env:ro,Z`), with the
instance enabled in the image (`systemctl enable lal-mcp@fleet` in a `RUN`).

## Without systemd

If your image's main process isn't systemd, start the launcher from your own entrypoint
or supervisor, as user `lal-mcp`. It reads the same settings as environment variables:

```bash
runuser -u lal-mcp -- env LAL_MCP_WORKSPACE=/ws LAL_MCP_HOST=0.0.0.0 \
    LAL_MCP_TOKEN_FILE=/run/secrets/lal-mcp-token /opt/lal-mcp/lal-mcp-serve
```

Or make it the container's process:

```bash
podman run -d --name mission-ada --user 990:990 \
  -e LAL_MCP_WORKSPACE=/ws -e LAL_MCP_HOST=0.0.0.0 \
  -e LAL_MCP_TOKEN_FILE=/run/secrets/lal-mcp-token \
  -v /srv/repos/mission:/ws:ro,Z -p 127.0.0.1:8765:8765 \
  --secret lal-mcp-token,type=mount,uid=990,gid=990,mode=0400 \
  --entrypoint /opt/lal-mcp/lal-mcp-serve --restart=on-failure <your-image>
```

The server logs to stderr: `podman logs`.

## On a host

`install.sh` also works on a RHEL 10 host (or VM) with GNAT Pro and Libadalang
installed. As root, from a lal-mcp checkout:

```bash
dnf -y install python3 python3-pip util-linux shadow-utils
deploy/install.sh --gnatpro-prefix /opt/gnatpro/26.1
cp /etc/lal-mcp/example.env.sample /etc/lal-mcp/mission.env
chmod 600 /etc/lal-mcp/mission.env
vi /etc/lal-mcp/mission.env             # LAL_MCP_WORKSPACE at least; a token if not 127.0.0.1
systemctl enable --now lal-mcp@mission
curl -s http://127.0.0.1:8765/healthz
journalctl -u lal-mcp@mission -f
```

Running `install.sh` again upgrades the server and keeps the `/etc/lal-mcp/*.env` files.
The `lal-mcp` user needs read access to the repository. It never writes to it.

## Settings

Set these in `/etc/lal-mcp/<name>.env`: one `VAR=value` per line, no `export`.

| variable | meaning |
| --- | --- |
| `LAL_MCP_WORKSPACE` | repository root (required). The server runs there and uses its `.lal-mcp.toml`; answers give paths relative to it |
| `LAL_MCP_ARGS` | extra server arguments, split like a shell command line. For a path with spaces, wrap the whole value in double quotes and the path in single quotes: `LAL_MCP_ARGS="-P '/srv/my repo/x.gpr'"`. Arguments: `-P /ws/x.gpr`, `-XVAR=value`, `--target`, `--RTS`, `--max-chars`, `--timeout`, `--tests-project`, `--log-level DEBUG` (see the README) |
| `LAL_MCP_HOST` | bind address. `127.0.0.1` (default) is local only; any other address requires a token |
| `LAL_MCP_PORT` | port (default `8765`) |
| `LAL_MCP_TOKEN` | the bearer token, or |
| `LAL_MCP_TOKEN_FILE` | a file holding it, readable by `lal-mcp` |
| `GNATPRO_PREFIX` | GNAT Pro prefix (default `/opt/gnatpro/26.1`). Its `bin` goes first on `PATH`, so GPR finds the toolchain and the Ada runtime |

After editing it: `systemctl restart lal-mcp@<name>`. The service exits with status 2 on
a configuration error (no project found, token missing for a non-local address, bad
path). It isn't restarted then: fix the file and start it again.

Source changes need no restart: the server re-parses files whose modification time
changes, and notices added or removed files and `.gpr` edits before each call.

## Clients

Codex (`~/.codex/config.toml`), with the token in the environment variable
`LAL_MCP_TOKEN` where Codex runs:

```toml
[mcp_servers.ada]
url = "http://<host>:8765/mcp"
bearer_token_env_var = "LAL_MCP_TOKEN"
tool_timeout_sec = 120
```

Claude Code:

```bash
claude mcp add --transport http ada http://<host>:8765/mcp -H "Authorization: Bearer $LAL_MCP_TOKEN"
```

If clients reach the server through a TLS proxy, point them at the proxy's URL; the server
itself speaks plain HTTP.

## Air-gapped builds

Nothing is downloaded at run time. At build time, the `RUN` needs this repository (a
`git clone` from your mirror) and the Python packages (`mcp` and its dependencies), from
pip's index or from a wheelhouse. Make one on a
machine with internet access, with the same Python as UBI 10 (3.12, x86_64). The surest
way is a UBI image:

```bash
mkdir -p wheelhouse
podman run --rm -v "$PWD/wheelhouse:/out:Z" registry.access.redhat.com/ubi10/ubi:10.2 \
  bash -c "dnf -y -q install python3-pip && pip3 download -q -d /out mcp==2.3.0"
```

Put `wheelhouse/` in your build context, mount it into the `RUN`, and pass it to
`install.sh`:

```dockerfile
RUN --mount=type=bind,source=wheelhouse,target=/tmp/wheelhouse     dnf -y install --setopt=install_weak_deps=False git-core python3 python3-pip util-linux shadow-utils     && dnf clean all     && git clone --depth 1 https://git.internal.example/tools/libadalang-mcp.git /tmp/lal-mcp     && /tmp/lal-mcp/deploy/install.sh --gnatpro-prefix /opt/gnatpro/26.1 --enable default          --wheelhouse /tmp/wheelhouse     && rm -rf /tmp/lal-mcp /root/.cache
```

The clone URL is your internal mirror, and the dnf packages come from your dnf mirror.

## With Docker instead of podman

The image works with Docker too, with two differences:

- **Running systemd as init:** Docker needs
  `--privileged --cgroupns=host -v /sys/fs/cgroup:/sys/fs/cgroup:rw` for it.
- **The token:** `docker run` has no `--secret`. A token file bind-mounted under
  `/run/secrets` is hidden when systemd mounts its own `/run`, so mount it elsewhere and
  point the env file at it:
  - `-v ./lal-mcp-token:/etc/lal-mcp/token:ro`
  - `LAL_MCP_TOKEN_FILE=/etc/lal-mcp/token` in the env file you mount over
    `/etc/lal-mcp/default.env`
  - The file must be readable by uid 990.

## Troubleshooting

| symptom | cause, fix |
| --- | --- |
| build fails: `no GNAT Pro in ...` | `--gnatpro-prefix` isn't where your image has GNAT Pro (its `bin/` must contain `gnat`) |
| build fails: `no Libadalang wheel under ...` | Libadalang is in another prefix: add `--libadalang-prefix` |
| build fails: `... is older than 3.11` | the image's `python3` is too old: pass `--python python3.12` (or newer) |
| build fails at `git clone` | no network to GitHub: clone from your internal mirror |
| `journalctl` shows `status=2` and `not configured` | no `.lal-mcp.toml` in `LAL_MCP_WORKSPACE`, and no `-P` in `LAL_MCP_ARGS` |
| `status=2`, `--host ... add --token-env` | non-local `LAL_MCP_HOST` without a token. Set `LAL_MCP_TOKEN` or `LAL_MCP_TOKEN_FILE` |
| `LAL_MCP_TOKEN_FILE ... is not readable` | the secret isn't readable by uid 990: add `uid=990,gid=990,mode=0400` to `--secret` |
| `LAL_MCP_WORKSPACE ... is not a directory` | the repository isn't mounted at that path |
| `status=226/NAMESPACE` | you enabled the commented sandboxing options in the unit inside a container: turn them off again |
| clients get `401` | wrong or missing token. Test with `curl -H "Authorization: Bearer $(cat lal-mcp-token)" ...` |
| `/healthz` returns 503 | the project didn't load: `journalctl -u lal-mcp@<name>` has the reason (often Libadalang failed to import) |
| answers start with `can't find a toolchain`; runtime units such as `Ada.Text_IO` don't resolve | `GNATPRO_PREFIX` in the env file is wrong: its `bin/` must contain `gnat` |
| permission denied reading sources | SELinux: mount with `:Z`. Otherwise give uid 990 read access |
| memory grows with the project | expected: Libadalang keeps parsed units. Cap it with `MemoryMax=` in the unit, or `--memory` on `podman run` |
