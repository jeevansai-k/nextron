#!/usr/bin/env bash
#
# NEXTRON — prerequisites installer
#
#   Installs the system packages NEXTRON drives, builds a private virtual
#   environment, installs NEXTRON into it, and puts a `nextron` launcher on
#   your PATH so the command works from any directory.
#
# Usage:
#   ./scripts/install-prerequisites.sh              # everything (asks for sudo)
#   ./scripts/install-prerequisites.sh --yes         # no prompts
#   ./scripts/install-prerequisites.sh --system      # launcher in /usr/local/bin
#   ./scripts/install-prerequisites.sh --skip-packages
#   ./scripts/install-prerequisites.sh --blocklists  # also download the lists
#   ./scripts/install-prerequisites.sh --dry-run     # print, change nothing
#
# Safe to re-run: every step checks before it acts.

set -euo pipefail

# --------------------------------------------------------------------------- #
# Brand colours (the NEXTRON palette), disabled when not writing to a terminal
# --------------------------------------------------------------------------- #

if [ -t 1 ] && [ -z "${NO_COLOR:-}" ]; then
    ACCENT=$'\033[38;2;155;89;182m'
    SURFACE=$'\033[38;2;200;162;200m'
    TEXT=$'\033[38;2;230;230;250m'
    PRIMARY=$'\033[38;2;155;89;182m\033[1m'
    RESET=$'\033[0m'
    BOLD=$'\033[1m'
else
    ACCENT=""; SURFACE=""; TEXT=""; PRIMARY=""; RESET=""; BOLD=""
fi

say()   { printf '%s\n' "${TEXT}$*${RESET}"; }
step()  { printf '\n%s\n' "${PRIMARY}▸ $*${RESET}"; }
note()  { printf '%s\n' "${SURFACE}  $*${RESET}"; }
ok()    { printf '%s\n' "${ACCENT}  ✔ $*${RESET}"; }
warn()  { printf '%s\n' "${SURFACE}  ! $*${RESET}"; }
die()   { printf '\n%s\n' "${PRIMARY}✖ $*${RESET}" >&2; exit 1; }

# --------------------------------------------------------------------------- #
# Arguments
# --------------------------------------------------------------------------- #

ASSUME_YES=0
SKIP_PACKAGES=0
SYSTEM_LAUNCHER=0
NO_LAUNCHER=0
DRY_RUN=0
FETCH_BLOCKLISTS=-1        # -1 = ask, 0 = no, 1 = yes

while [ $# -gt 0 ]; do
    case "$1" in
        -y|--yes)          ASSUME_YES=1 ;;
        --skip-packages)   SKIP_PACKAGES=1 ;;
        --system)          SYSTEM_LAUNCHER=1 ;;
        --no-launcher)     NO_LAUNCHER=1 ;;
        --blocklists)      FETCH_BLOCKLISTS=1 ;;
        --no-blocklists)   FETCH_BLOCKLISTS=0 ;;
        --dry-run)         DRY_RUN=1 ;;
        -h|--help)
            sed -n '2,20p' "$0" | sed 's/^# \{0,1\}//'
            exit 0 ;;
        *) die "Unknown option: $1 (try --help)" ;;
    esac
    shift
done

run() {
    if [ "$DRY_RUN" -eq 1 ]; then
        printf '%s\n' "${SURFACE}  would run: $*${RESET}"
    else
        "$@"
    fi
}

confirm() {
    [ "$ASSUME_YES" -eq 1 ] && return 0
    [ "$DRY_RUN" -eq 1 ] && return 0
    printf '%s' "${TEXT}  $1 [Y/n] ${RESET}"
    read -r reply || reply="n"
    case "$reply" in ""|y|Y|yes|YES) return 0 ;; *) return 1 ;; esac
}

# --------------------------------------------------------------------------- #
# Banner
# --------------------------------------------------------------------------- #

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_DIR"

printf '%s\n' "${ACCENT}${BOLD}"
cat <<'BANNER'
    )
 ( /(                )
 )\())   (     )  ( /( (
((_)\   ))\ ( /(  )\()))(    (    (
 _((_) /((_))\())(_))/(()\   )\   )\ )
| \| |(_)) ((_)\ | |_  ((_) ((_) _(_/(
| .` |/ -_)\ \ / |  _|| '_|/ _ \| ' \))
|_|\_|\___|/_\_\  \__||_|  \___/|_||_|
BANNER
printf '%s\n' "${RESET}${SURFACE}     Intelligent Tor Switcher with VPN & Ad Blocking in CLI${RESET}"
say ""
note "project: $PROJECT_DIR"
[ "$DRY_RUN" -eq 1 ] && warn "dry run: nothing will be changed"

# --------------------------------------------------------------------------- #
# 1. Python
# --------------------------------------------------------------------------- #

step "Checking Python"

PYTHON=""
for candidate in python3.13 python3.12 python3.11 python3; do
    if command -v "$candidate" >/dev/null 2>&1; then
        if "$candidate" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)'; then
            PYTHON="$candidate"
            break
        fi
    fi
done
[ -n "$PYTHON" ] || die "Python 3.11 or newer is required. Install it, then re-run this script."
ok "$($PYTHON --version) at $(command -v "$PYTHON")"

# --------------------------------------------------------------------------- #
# 2. System packages
# --------------------------------------------------------------------------- #

step "System packages"

if [ "$SKIP_PACKAGES" -eq 1 ]; then
    note "skipped (--skip-packages)"
else
    PKG_MANAGER=""
    for candidate in apt-get dnf pacman zypper; do
        command -v "$candidate" >/dev/null 2>&1 && { PKG_MANAGER="$candidate"; break; }
    done

    # tor + openvpn + wireguard are what NEXTRON drives; nftables/iptables back
    # the kill switch and transparent routing; zenity is the native file browser.
    case "$PKG_MANAGER" in
        apt-get)
            PACKAGES="tor openvpn wireguard-tools iproute2 iptables nftables curl ca-certificates zenity python3-venv python3-pip"
            INSTALL="sudo apt-get install -y"
            REFRESH="sudo apt-get update" ;;
        dnf)
            PACKAGES="tor openvpn wireguard-tools iproute iptables nftables curl ca-certificates zenity python3-pip"
            INSTALL="sudo dnf install -y"
            REFRESH="" ;;
        pacman)
            PACKAGES="tor openvpn wireguard-tools iproute2 iptables nftables curl ca-certificates zenity python-pip"
            INSTALL="sudo pacman -S --needed --noconfirm"
            REFRESH="sudo pacman -Sy" ;;
        zypper)
            PACKAGES="tor openvpn wireguard-tools iproute2 iptables nftables curl ca-certificates zenity python3-pip"
            INSTALL="sudo zypper install -y"
            REFRESH="sudo zypper refresh" ;;
        *)
            PACKAGES=""
            warn "no supported package manager found (apt/dnf/pacman/zypper)"
            note "install these yourself: tor openvpn wireguard-tools iproute2 iptables nftables curl ca-certificates zenity" ;;
    esac

    if [ -n "$PACKAGES" ]; then
        note "manager: $PKG_MANAGER"
        note "packages: $PACKAGES"
        if confirm "Install them now?"; then
            [ -n "$REFRESH" ] && run $REFRESH
            # A single failing package must not abort the whole install; the
            # doctor report at the end names anything still missing.
            if ! run $INSTALL $PACKAGES; then
                warn "some packages could not be installed"
                note "run '$INSTALL <package>' for each one the doctor reports"
            elif [ "$DRY_RUN" -eq 0 ]; then
                ok "system packages installed"
            fi
        else
            warn "skipped; NEXTRON will report the missing pieces"
        fi
    fi
fi

# --------------------------------------------------------------------------- #
# 3. Virtual environment
# --------------------------------------------------------------------------- #

step "Virtual environment"

VENV="$PROJECT_DIR/.venv"
if [ -x "$VENV/bin/python" ]; then
    ok "reusing $VENV"
else
    note "creating $VENV"
    run "$PYTHON" -m venv "$VENV" \
        || die "Could not create the virtual environment. On Debian/Ubuntu: sudo apt install python3-venv"
    ok "created"
fi

step "Installing NEXTRON and its dependencies"
if [ "$DRY_RUN" -eq 1 ]; then
    note "would run: $VENV/bin/python -m pip install --upgrade pip"
    note "would run: $VENV/bin/python -m pip install -e $PROJECT_DIR"
else
    "$VENV/bin/python" -m pip install --quiet --upgrade pip
    "$VENV/bin/python" -m pip install --quiet -e "$PROJECT_DIR"
    ok "$("$VENV/bin/nextron" --version)"
fi

# --------------------------------------------------------------------------- #
# 4. The `nextron` command
# --------------------------------------------------------------------------- #

step "Installing the 'nextron' command"

if [ "$NO_LAUNCHER" -eq 1 ]; then
    note "skipped (--no-launcher)"
    note "run it with: $VENV/bin/nextron"
else
    if [ "$SYSTEM_LAUNCHER" -eq 1 ]; then
        LAUNCHER_DIR="/usr/local/bin"
        NEEDS_SUDO=1
    else
        LAUNCHER_DIR="$HOME/.local/bin"
        NEEDS_SUDO=0
    fi
    LAUNCHER="$LAUNCHER_DIR/nextron"

    LAUNCHER_BODY="#!/bin/sh
# NEXTRON launcher — generated by scripts/install-prerequisites.sh
# Runs NEXTRON from its own virtual environment, from any directory.
VENV=\"$VENV\"
if [ ! -x \"\$VENV/bin/nextron\" ]; then
    echo \"NEXTRON is not installed in \$VENV\" >&2
    echo \"Re-run: $PROJECT_DIR/scripts/install-prerequisites.sh\" >&2
    exit 1
fi
exec \"\$VENV/bin/nextron\" \"\$@\"
"

    if [ "$DRY_RUN" -eq 1 ]; then
        note "would write $LAUNCHER"
    elif [ "$NEEDS_SUDO" -eq 1 ]; then
        printf '%s' "$LAUNCHER_BODY" | sudo tee "$LAUNCHER" >/dev/null
        sudo chmod 755 "$LAUNCHER"
        ok "installed $LAUNCHER (available to sudo too)"
    else
        mkdir -p "$LAUNCHER_DIR"
        printf '%s' "$LAUNCHER_BODY" > "$LAUNCHER"
        chmod 755 "$LAUNCHER"
        ok "installed $LAUNCHER"
    fi

    case ":${PATH}:" in
        *":$LAUNCHER_DIR:"*)
            [ "$DRY_RUN" -eq 1 ] || ok "$LAUNCHER_DIR is already on your PATH" ;;
        *)
            warn "$LAUNCHER_DIR is not on your PATH"
            note "add this line to ~/.bashrc (or ~/.zshrc), then open a new terminal:"
            printf '%s\n' "${TEXT}      export PATH=\"\$HOME/.local/bin:\$PATH\"${RESET}" ;;
    esac
fi

# --------------------------------------------------------------------------- #
# 5. The Sources drop-box
# --------------------------------------------------------------------------- #

step "Sources folder"

if [ "$DRY_RUN" -eq 1 ]; then
    note "would create the Sources drop-box"
else
    "$VENV/bin/nextron" config path >/dev/null 2>&1 || true
fi

note "Copy your VPN profiles and filter lists into:"
note "  $HOME/.config/nextron/Sources/VPN Profiles"
note "  $HOME/.config/nextron/Sources/DNS list"
note "NEXTRON imports them on launch. Nothing is bundled and nothing is moved."

step "Ad-blocking lists"

note "The catalogue holds the addresses of the filter lists; the lists"
note "themselves are downloaded from their publishers, which takes about a"
note "minute and contacts each site directly."

DO_FETCH=0
if [ "$FETCH_BLOCKLISTS" -eq 1 ]; then
    DO_FETCH=1
elif [ "$FETCH_BLOCKLISTS" -eq -1 ] && [ "$ASSUME_YES" -eq 0 ]; then
    confirm "Download them now?" && DO_FETCH=1
fi

if [ "$DRY_RUN" -eq 1 ]; then
    if [ "$DO_FETCH" -eq 1 ]; then
        note "would run: $VENV/bin/nextron dns update"
    else
        note "would skip the download"
    fi
elif [ "$DO_FETCH" -eq 1 ]; then
    "$VENV/bin/nextron" dns update || warn "some lists could not be downloaded"
else
    note "skipped — download them later with: nextron dns update"
fi

# --------------------------------------------------------------------------- #
# 6. Health report
# --------------------------------------------------------------------------- #

step "Health report"
if [ "$DRY_RUN" -eq 1 ]; then
    note "would run: $VENV/bin/nextron doctor"
else
    "$VENV/bin/nextron" doctor || true
fi

# --------------------------------------------------------------------------- #
# Done
# --------------------------------------------------------------------------- #

printf '\n%s\n' "${ACCENT}${BOLD}NEXTRON is ready.${RESET}"
say ""
say "  ${BOLD}nextron${RESET}${TEXT}                  launch the interface"
say "  ${BOLD}nextron doctor${RESET}${TEXT}           re-check this machine"
say "  ${BOLD}nextron vpn list${RESET}${TEXT}         the VPN profiles you imported"
say "  ${BOLD}nextron dns update${RESET}${TEXT}       download / refresh the ad-blocking lists"
say "  ${BOLD}nextron vpn import${RESET}${TEXT}       pick more VPN profiles in your file browser"
say ""
if [ "$SYSTEM_LAUNCHER" -eq 1 ]; then
    say "  For VPN tunnels, the kill switch and DNS on port 53:"
    say "  ${BOLD}sudo nextron${RESET}"
else
    say "  For VPN tunnels, the kill switch and DNS on port 53:"
    say "  ${BOLD}sudo $VENV/bin/nextron${RESET}"
    note "(or re-run this script with --system to make 'sudo nextron' work)"
fi
say ""
