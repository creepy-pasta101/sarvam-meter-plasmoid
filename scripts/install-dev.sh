#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
#
# Install sarvam-meter for the *current user* only: the Python package, the
# `sarvam-meter` console script and the Plasma applet.
#
#   ./scripts/install-dev.sh
#
# No root, no sudo, no network access. Idempotent: re-running upgrades both the
# package and the applet in place. Nothing is written outside $HOME or a temp
# directory.

set -euo pipefail

REPO_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
PLASMOID_ID="io.github.creepy_pasta101.sarvammeter"
PLASMOID_DIR="${REPO_ROOT}/plasmoid/${PLASMOID_ID}"
USER_BIN="${HOME}/.local/bin"

log()  { printf '[install-dev] %s\n' "$*"; }
warn() { printf '[install-dev] WARNING: %s\n' "$*" >&2; }
die()  { printf '[install-dev] ERROR: %s\n' "$*" >&2; exit 1; }

# --- preconditions ---------------------------------------------------------
command -v python3 >/dev/null 2>&1 || die "python3 is required but was not found in PATH"
[ -d "${REPO_ROOT}/sarvam_meter" ] || die "sarvam_meter/ was not found next to this script (${REPO_ROOT})"
[ -f "${PLASMOID_DIR}/metadata.json" ] || die "plasmoid package not found at ${PLASMOID_DIR}"

# --- 1. Python package -----------------------------------------------------
installed=0
if python3 -m pip --version >/dev/null 2>&1; then
    log "installing the sarvam-meter package for the current user (pip --user)"
    if python3 -m pip install --user --upgrade "${REPO_ROOT}"; then
        installed=1
    else
        warn "pip install --user failed; falling back to a plain file copy"
    fi
else
    log "pip is not available; falling back to a plain file copy"
fi

if [ "${installed}" -eq 0 ]; then
    user_site="$(python3 -c 'import site; print(site.getusersitepackages())')"
    [ -n "${user_site}" ] || die "could not determine the user site-packages directory"
    log "copying sarvam_meter into ${user_site}"
    mkdir -p "${user_site}"
    rm -rf "${user_site:?}/sarvam_meter"
    cp -a "${REPO_ROOT}/sarvam_meter" "${user_site}/sarvam_meter"
    mkdir -p "${USER_BIN}"
    cat > "${USER_BIN}/sarvam-meter" <<'SH'
#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Console entry point for the sarvam-meter daemon and CLI."""
import sys

from sarvam_meter.cli import main

if __name__ == "__main__":
    sys.exit(main())
SH
    chmod 0755 "${USER_BIN}/sarvam-meter"
    log "wrote ${USER_BIN}/sarvam-meter"
    case ":${PATH}:" in
        *":${USER_BIN}:"*) ;;
        *) warn "${USER_BIN} is not in PATH; add it to use the sarvam-meter command" ;;
    esac
fi

# --- 2. Plasma applet ------------------------------------------------------
kpackagetool=""
for candidate in kpackagetool6 kpackagetool5; do
    if command -v "${candidate}" >/dev/null 2>&1; then
        kpackagetool="${candidate}"
        break
    fi
done

if [ -z "${kpackagetool}" ]; then
    warn "neither kpackagetool6 nor kpackagetool5 was found; skipping the applet."
    warn "install plasma-workspace (Plasma 6) and re-run this script."
else
    if "${kpackagetool}" --type Plasma/Applet --list 2>/dev/null | grep -qx -- "${PLASMOID_ID}"; then
        log "upgrading the ${PLASMOID_ID} applet with ${kpackagetool}"
        "${kpackagetool}" --type Plasma/Applet --upgrade "${PLASMOID_DIR}"
    else
        log "installing the ${PLASMOID_ID} applet with ${kpackagetool}"
        "${kpackagetool}" --type Plasma/Applet --install "${PLASMOID_DIR}"
    fi
fi

# --- 3. What the user still has to do themselves ---------------------------
log "done."
cat <<'HINT'

Remaining steps (this script never restarts your session or the desktop):

  * Restart plasmashell so it picks up a changed applet:
        kquitapp6 plasmashell && kstart plasmashell      # Plasma 6
    (or simply log out and back in)

  * Write a default config and set a starting balance:
        sarvam-meter init
        sarvam-meter balance set 500

  * Run the daemon in the foreground, or as a user service:
        sarvam-meter serve --with-proxy
        install -D -m 0644 packaging/sarvam-meter.service \
            ~/.config/systemd/user/sarvam-meter.service
        systemctl --user daemon-reload
        systemctl --user enable --now sarvam-meter.service

HINT
