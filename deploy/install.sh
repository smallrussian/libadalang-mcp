#!/bin/bash
# Install lal-mcp as a systemd service on RHEL / UBI (run as root, from a checkout of this repo).
#
#   deploy/install.sh [--gnatpro-prefix DIR] [--libadalang-prefix DIR] [--vendor DIR]
#                     [--wheelhouse DIR] [--prefix DIR] [--uid N] [--python PYTHON]
#
# Meant to run in your own image's build (a RUN step) or on a host: it uses the GNAT Pro
# and Libadalang already installed there, and installs only the server.
#
#   --gnatpro-prefix DIR  GNAT Pro install prefix, the one whose bin/ has gnat
#                         (default /opt/gnatpro/26.1)
#   --libadalang-prefix DIR  Libadalang install prefix, if not the same as GNAT Pro's; the
#                         wheel is <DIR>/share/libadalang/python/libadalang-*.whl
#
#   --vendor DIR          only if GNAT Pro / Libadalang aren't installed yet: folder with the
#                         AdaCore archives (AdaCore-gnat-*-x86_64-linux.zip and
#                         AdaCore-libadalang-*-x86_64-linux.zip, or the *-bin.tar.gz inside
#                         them). Installs the missing ones into the prefixes above.
#   --wheelhouse DIR      install the Python packages from DIR, offline (see docs/linux-service.md
#                         for making one). Without it, pip uses its index.
#   --prefix DIR          where the server goes (default /opt/lal-mcp)
#   --uid N               uid/gid of the lal-mcp system user (default 990)
#   --python PYTHON       interpreter for the venv, 3.11 or newer (default python3)
#   --enable NAME         also set up and enable the instance lal-mcp@NAME for a container:
#                         writes /etc/lal-mcp/NAME.env (unless it exists) with the repository
#                         at /ws, listening on all interfaces, token from the podman secret
#                         /run/secrets/lal-mcp-token; then systemctl enable lal-mcp@NAME.
#                         Mount your own env file over it at run time to change anything.
#
# Re-running it upgrades the server in place and keeps /etc/lal-mcp/*.env.
set -euo pipefail

ENABLE="" VENDOR="" WHEELHOUSE="" GNATPRO_PREFIX=/opt/gnatpro/26.1 LAL_PREFIX="" PREFIX=/opt/lal-mcp UID_GID=990 PYTHON=python3
while [ $# -gt 0 ]; do
    case "$1" in
        --vendor) VENDOR="$2"; shift 2 ;;
        --wheelhouse) WHEELHOUSE="$2"; shift 2 ;;
        --gnatpro-prefix) GNATPRO_PREFIX="$2"; shift 2 ;;
        --libadalang-prefix) LAL_PREFIX="$2"; shift 2 ;;
        --prefix) PREFIX="$2"; shift 2 ;;
        --uid) UID_GID="$2"; shift 2 ;;
        --python) PYTHON="$2"; shift 2 ;;
        --enable) ENABLE="$2"; shift 2 ;;
        -h|--help) sed -n '2,35p' "$0"; exit 0 ;;
        *) echo "install.sh: unknown argument $1 (see --help)" >&2; exit 2 ;;
    esac
done
LAL_PREFIX="${LAL_PREFIX:-$GNATPRO_PREFIX}"

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
step() { echo "==> $*"; }
die() { echo "install.sh: $*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || die "run as root"
command -v "$PYTHON" >/dev/null || die "$PYTHON not found (dnf install python3 python3-pip)"
"$PYTHON" -c 'import sys; sys.exit(sys.version_info < (3, 11))' || die "$PYTHON is older than 3.11"
"$PYTHON" -c 'import venv, ensurepip' 2>/dev/null || die "$PYTHON can't make venvs (dnf install python3-pip)"
[ -f "$SRC/lal_mcp/server.py" ] || die "run from a checkout of lal-mcp ($SRC has no lal_mcp/)"

# --- GNAT Pro + Libadalang -------------------------------------------------------------
# doinstall <prefix> installs non-interactively. Both go into the same prefix, so one PATH
# entry covers the toolchain and the Libadalang wheel sits under it.
install_adacore() {  # $1 = package name (gnat or libadalang), $2 = install prefix
    local name="$1" dest="$2" archive tmp tarball
    archive=$(ls "$VENDOR"/AdaCore-"$name"-*-x86_64-linux.zip "$VENDOR"/*"$name"*-x86_64-linux-bin.tar.gz \
              2>/dev/null | head -n1 || true)
    [ -n "$archive" ] || die "no $name archive (x86_64-linux zip or -bin.tar.gz) in $VENDOR"
    tmp=$(mktemp -d)
    step "unpacking $(basename "$archive")"
    if [[ "$archive" == *.zip ]]; then  # UBI has no unzip; Python does it
        "$PYTHON" -m zipfile -e "$archive" "$tmp/zip"
        tarball=$(ls "$tmp"/zip/*/*-x86_64-linux-bin.tar.gz | head -n1)
    else
        tarball="$archive"
    fi
    tar -xzf "$tarball" -C "$tmp"
    step "installing $name into $dest"
    if ! (cd "$tmp"/*-x86_64-linux-bin && ./doinstall "$dest" >"$tmp/doinstall.log" 2>&1); then
        tail -n 15 "$tmp/doinstall.log" >&2
        rm -rf "$tmp"
        die "$name doinstall failed (it needs make and clear: dnf install make ncurses)"
    fi
    rm -rf "$tmp"
}
if [ -n "$VENDOR" ]; then
    for tool in make clear tar gzip; do
        command -v "$tool" >/dev/null || die "$tool not found: AdaCore's doinstall needs it (dnf install make ncurses tar gzip)"
    done
    if [ -x "$GNATPRO_PREFIX/bin/gnat" ]; then
        step "GNAT Pro already in $GNATPRO_PREFIX, skipping it"
    else
        install_adacore gnat "$GNATPRO_PREFIX"
    fi
    if ls "$LAL_PREFIX"/share/libadalang/python/libadalang-*.whl >/dev/null 2>&1; then
        step "Libadalang already in $LAL_PREFIX, skipping it"
    else
        install_adacore libadalang "$LAL_PREFIX"
    fi
fi
[ -x "$GNATPRO_PREFIX/bin/gnat" ] || die "no GNAT Pro in $GNATPRO_PREFIX (pass --gnatpro-prefix, or --vendor to install it)"
WHEEL=$(ls "$LAL_PREFIX"/share/libadalang/python/libadalang-*.whl 2>/dev/null | head -n1 || true)
[ -n "$WHEEL" ] || die "no Libadalang wheel under $LAL_PREFIX/share/libadalang/python (pass --libadalang-prefix)"

# --- service user ------------------------------------------------------------------------
if ! id lal-mcp >/dev/null 2>&1; then
    step "creating system user lal-mcp (uid $UID_GID)"
    getent group lal-mcp >/dev/null || groupadd --system --gid "$UID_GID" lal-mcp
    useradd --system --uid "$UID_GID" --gid lal-mcp --home-dir /var/lib/lal-mcp --no-create-home \
            --shell /sbin/nologin --comment "Libadalang MCP server" lal-mcp
fi

# --- server and venv -----------------------------------------------------------------------
step "installing the server into $PREFIX"
install -d -m 0755 "$PREFIX" "$PREFIX/app" "$PREFIX/app/docs"
rm -rf "$PREFIX/app/lal_mcp"
cp -r "$SRC/lal_mcp" "$PREFIX/app/"
find "$PREFIX/app/lal_mcp" -name __pycache__ -prune -exec rm -rf {} +
install -m 0644 "$SRC/requirements.txt" "$PREFIX/app/"
install -m 0644 "$SRC/docs/linux-service.md" "$PREFIX/app/docs/"
install -m 0755 "$SRC/deploy/lal-mcp-serve" "$PREFIX/lal-mcp-serve"

step "creating the venv $PREFIX/venv"
[ -x "$PREFIX/venv/bin/python" ] || "$PYTHON" -m venv "$PREFIX/venv"
pip_args=(--disable-pip-version-check -q)
if [ -n "$WHEELHOUSE" ]; then
    pip_args+=(--no-index --find-links "$WHEELHOUSE")
fi
# mcp only: the service doesn't need pytest (requirements.txt pins both)
mcp_pin=$(grep -E '^mcp==' "$SRC/requirements.txt")
"$PREFIX/venv/bin/python" -m pip install "${pip_args[@]}" "$mcp_pin"
"$PREFIX/venv/bin/python" -m pip install "${pip_args[@]}" --force-reinstall --no-deps "$WHEEL"
chmod -R a+rX "$PREFIX"

step "checking the install as user lal-mcp"
runuser -u lal-mcp -- env -i HOME=/var/lib/lal-mcp PATH="$GNATPRO_PREFIX/bin:/usr/bin:/bin" \
    PYTHONPATH="$PREFIX/app" "$PREFIX/venv/bin/python" -c \
    'import libadalang, mcp, lal_mcp.server; print("    libadalang", libadalang.version, "| mcp ok")'

# --- systemd unit and config -----------------------------------------------------------------
step "installing the unit /etc/systemd/system/lal-mcp@.service"
install -m 0644 "$SRC/deploy/lal-mcp@.service" /etc/systemd/system/lal-mcp@.service
if [ "$PREFIX" != /opt/lal-mcp ]; then
    sed -i "s#/opt/lal-mcp/#$PREFIX/#g" /etc/systemd/system/lal-mcp@.service
fi
install -d -m 0755 /etc/lal-mcp
install -m 0644 "$SRC/deploy/lal-mcp.env.example" /etc/lal-mcp/example.env.sample
sed -i "s#^GNATPRO_PREFIX=.*#GNATPRO_PREFIX=$GNATPRO_PREFIX#" /etc/lal-mcp/example.env.sample
if [ -d /run/systemd/system ]; then
    systemctl daemon-reload
fi

if [ -n "$ENABLE" ]; then
    [[ "$ENABLE" =~ ^[A-Za-z0-9_.-]+$ ]] || die "--enable $ENABLE: use letters, digits, . _ -"
    command -v systemctl >/dev/null || die "--enable needs systemd (systemctl not found)"
    env_file="/etc/lal-mcp/$ENABLE.env"
    if [ -e "$env_file" ]; then
        step "keeping the existing $env_file"
    else
        step "writing $env_file (container defaults)"
        install -m 0600 /etc/lal-mcp/example.env.sample "$env_file"
        sed -i -e 's#^LAL_MCP_WORKSPACE=.*#LAL_MCP_WORKSPACE=/ws#' \
               -e 's#^LAL_MCP_HOST=.*#LAL_MCP_HOST=0.0.0.0#' \
               -e 's#^\#LAL_MCP_TOKEN_FILE=.*#LAL_MCP_TOKEN_FILE=/run/secrets/lal-mcp-token#' "$env_file"
    fi
    step "enabling lal-mcp@$ENABLE"
    systemctl enable "lal-mcp@$ENABLE" 2>&1 | sed 's/^/    /'
    cat <<EOF
==> done. lal-mcp@$ENABLE starts with systemd. At run time: mount the repository at /ws,
    give the token as the podman secret lal-mcp-token (uid=990), publish port 8765.
EOF
    exit 0
fi

cat <<EOF
==> done.
    Next, for each repository <name>:
      cp /etc/lal-mcp/example.env.sample /etc/lal-mcp/<name>.env && chmod 600 /etc/lal-mcp/<name>.env
      edit it (LAL_MCP_WORKSPACE at least), then:  systemctl enable --now lal-mcp@<name>
EOF
