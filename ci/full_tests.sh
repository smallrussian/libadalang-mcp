#!/usr/bin/env bash
# Full test run against a real GNAT Pro + Libadalang install (Linux).
# Used by both GitHub Actions and GitLab CI.
#
#   GNATPRO_PREFIX  install prefix of GNAT Pro + Libadalang (required), e.g. /opt/gnatpro/26.1
#   PYTHON          interpreter to build the venv from (default: python3)
#   REPORT_DIR      where junit/smoke/metrics files go (default: reports)
set -euo pipefail

: "${GNATPRO_PREFIX:?set GNATPRO_PREFIX to the GNAT Pro + Libadalang install prefix}"
PYTHON="${PYTHON:-python3}"
REPORT_DIR="${REPORT_DIR:-reports}"
mkdir -p "$REPORT_DIR"

wheel=$(ls "$GNATPRO_PREFIX"/share/libadalang/python/libadalang-*.whl 2>/dev/null | head -n1 || true)
if [ -z "$wheel" ]; then
    echo "no libadalang wheel under $GNATPRO_PREFIX/share/libadalang/python" >&2
    exit 1
fi

venv="${VENV_DIR:-.venv-ci}"
"$PYTHON" -m venv "$venv"
"$venv/bin/python" -m pip install -q -r requirements.txt "$wheel"

export PATH="$GNATPRO_PREFIX/bin:$PATH"   # GPR finds the toolchain / Ada runtime via PATH
export LAL_REQUIRED=1                     # fail, don't skip, if libadalang can't be imported

"$venv/bin/python" -c "import libadalang as l; print('libadalang', l.version)"
"$venv/bin/python" -m pytest -p no:cacheprovider --junitxml="$REPORT_DIR/junit-full.xml" -rs
"$venv/bin/python" scripts/smoke.py | tee "$REPORT_DIR/smoke-fixture.txt"
