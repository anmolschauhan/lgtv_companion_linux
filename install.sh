#!/usr/bin/env bash
# Install LGTV Companion for Linux and enable its service for a user.
#
#   sudo ./install.sh [--user NAME]    install, then enable and start the service for NAME
#                                      (default: the user who ran sudo)
#   sudo ./install.sh --uninstall      remove it again (your ~/.config settings are kept)
set -euo pipefail

LIB_DIR=/usr/local/lib/lgtv-companion
BIN=/usr/local/bin/lgtv-companion
UNIT=/etc/systemd/system/lgtv-companion@.service
SRC=$(cd "$(dirname "$0")" && pwd)

die() { echo "error: $*" >&2; exit 1; }

user=${SUDO_USER:-}
action=install
while [[ $# -gt 0 ]]; do
    case $1 in
        --user) [[ $# -ge 2 ]] || die "--user needs a name"; user=$2; shift 2 ;;
        --uninstall) action=uninstall; shift ;;
        -h|--help) sed -n '2,6s/^# \{0,1\}//p' "$0"; exit 0 ;;
        *) die "unknown argument: $1" ;;
    esac
done
[[ $EUID -eq 0 ]] || die "run this with sudo"

if [[ $action == uninstall ]]; then
    for link in /etc/systemd/system/*.wants/lgtv-companion@*.service; do
        if [[ -L $link ]]; then systemctl disable --now "$(basename "$link")"; fi
    done
    rm -rf "$LIB_DIR" "$BIN" "$UNIT"
    systemctl daemon-reload
    echo "Uninstalled. Settings remain in ~/.config/lgtv-companion/."
    exit 0
fi

[[ -n $user && $user != root ]] || die "can't tell which user to run as; pass --user NAME"
id "$user" >/dev/null 2>&1 || die "no such user: $user"
/usr/bin/python3 -c 'from gi.repository import Gio' 2>/dev/null \
    || die "PyGObject is missing; install it with: sudo apt install python3-gi"

rm -rf "$LIB_DIR"
install -d "$LIB_DIR/lgtv_companion"
install -m 644 "$SRC"/lgtv_companion/*.py "$LIB_DIR/lgtv_companion/"
cat > "$BIN" <<EOF
#!/usr/bin/python3
import sys
sys.path.insert(0, "$LIB_DIR")
from lgtv_companion.cli import main
sys.exit(main())
EOF
chmod 755 "$BIN"
install -m 644 "$SRC/systemd/lgtv-companion@.service" "$UNIT"
systemctl daemon-reload
systemctl enable "lgtv-companion@$user.service"
systemctl restart "lgtv-companion@$user.service"

echo
echo "Installed $BIN and enabled lgtv-companion@$user.service."
echo "If you haven't paired a TV yet, run (as $user):  lgtv-companion pair"
echo "Logs:  journalctl -u lgtv-companion@$user -f"
