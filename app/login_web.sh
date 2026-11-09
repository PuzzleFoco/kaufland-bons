#!/bin/sh
# One-shot login helper: virtual display + noVNC so the Kaufland login can be
# completed in a normal browser from another machine (2FA included).
set -eu

DISPLAY_NUM="${LOGIN_DISPLAY:-:99}"
SCREEN="${LOGIN_SCREEN:-1280x900x24}"
PORT="${LOGIN_PORT:-6080}"
KEEP="${LOGIN_KEEPALIVE:-600}"
CONFIG="${CONFIG_DIR:-/config}"
AUTH="${KAUFLAND_AUTH_FILE:-$CONFIG/auth.json}"
PASSWORD_FILE="$CONFIG/vnc-password.txt"

mkdir -p /tmp/.X11-unix "$CONFIG"

# VNC_PASSWORD=  leer      -> Zufallspasswort, Datei in /config (0600)
# VNC_PASSWORD=  <wert>    -> dieses Passwort
# VNC_PASSWORD=  none/off  -> gar keine Abfrage (nur in vertrauenswuerdigem LAN)
password="${VNC_PASSWORD:-}"
no_auth=0
case "$(printf '%s' "$password" | tr '[:upper:]' '[:lower:]')" in
  none|off|0|false|no) no_auth=1; password="" ;;
esac
if [ "$no_auth" -eq 0 ] && [ -z "$password" ]; then
  password="$(head -c 32 /dev/urandom | base64 | tr -dc 'A-Za-z0-9' | cut -c1-10)"
  umask 077
  printf '%s\n' "$password" > "$PASSWORD_FILE"
  chmod 600 "$PASSWORD_FILE"
fi

Xvfb "$DISPLAY_NUM" -screen 0 "$SCREEN" -nolisten tcp >/tmp/xvfb.log 2>&1 &
XVFB_PID=$!
sleep 1
export DISPLAY="$DISPLAY_NUM"

openbox >/tmp/openbox.log 2>&1 &
OPENBOX_PID=$!

if [ "$no_auth" -eq 1 ]; then
  x11vnc -display "$DISPLAY_NUM" -localhost -forever -shared \
         -nopw -rfbport 5900 -noxdamage >/tmp/x11vnc.log 2>&1 &
else
  x11vnc -storepasswd "$password" /tmp/vnc.pass >/dev/null 2>&1
  x11vnc -display "$DISPLAY_NUM" -localhost -forever -shared \
         -rfbauth /tmp/vnc.pass -rfbport 5900 -noxdamage >/tmp/x11vnc.log 2>&1 &
fi
VNC_PID=$!

websockify --web /usr/share/novnc "$PORT" localhost:5900 >/tmp/websockify.log 2>&1 &
WS_PID=$!

cleanup() {
  kill "$WS_PID" "$VNC_PID" "$OPENBOX_PID" "$XVFB_PID" 2>/dev/null || true
}
trap cleanup EXIT INT TERM

sleep 1
echo "=============================================================="
echo " Login-Fenster:  http://<IP-des-NAS>:$PORT/vnc.html?autoconnect=1&resize=scale"
if [ "$no_auth" -eq 1 ]; then
  echo " Passwort:       KEINS (VNC_PASSWORD=none) - jeder im LAN kann zusehen"
elif [ -n "${VNC_PASSWORD:-}" ]; then
  echo " Passwort:       aus VNC_PASSWORD"
else
  echo " Passwort:       steht in $PASSWORD_FILE (nicht im Log)"
fi
echo " Nach erfolgreichem Login wird auth.json geschrieben und der"
echo " Container beendet sich. Danach diesen Container entfernen."
echo "=============================================================="

if kaufland --auth-file "$AUTH" login \
     --country "${KAUFLAND_COUNTRY:-DE}" --language "${KAUFLAND_LANGUAGE:-de}"; then
  rm -f "$PASSWORD_FILE"
  echo "Login gespeichert: $AUTH"
  echo "Container beendet sich in 15 s."
  sleep 15
  exit 0
fi

echo "Login nicht abgeschlossen. Fenster bleibt ${KEEP}s offen (LOGIN_KEEPALIVE)." >&2
sleep "$KEEP"
exit 1
