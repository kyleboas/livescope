#!/usr/bin/env bash
# Install livescope as a system service that gets its OpenSky API client from agent-secrets.
#
#   sudo secret livescope opensky-client-id
#   sudo secret livescope opensky-client-secret
#   sudo bash deploy/install-service.sh       # run again after adding or changing the secrets
#
# FAA SWIM flight data (optional, see swim.py), per subscription (tfms, sfdps) from the SWIFT Portal:
#   sudo secret livescope swim-tfms-username; sudo secret livescope swim-tfms-password; sudo secret livescope swim-tfms-queue
# It also installs Solace's Python library (solace-pubsubplus, checked against its PyPI hash) in /opt/livescope-pylib.
#
# The secrets stay root-only in /etc/agent-secrets/projects/livescope/. systemd hands them to the service
# (LoadCredential) at start. The service runs as its own throwaway user (DynamicUser), not as the agent
# user, sees only the scope and livescope checkouts read-only, and writes only /var/lib/livescope
# (ground maps and timelapses). Optional settings (LIVESCOPE_TIMELAPSE_KEY) go in /etc/livescope.env.
# It also installs livescope-update.timer, which pulls both checkouts every minute and restarts the site
# when either changed, so pushing to GitHub deploys.
set -euo pipefail
command -v systemctl >/dev/null && command -v getent >/dev/null || {
  echo "This installs the site's Linux server (systemd). Run it there (for livescope: ssh coding), not on this machine." >&2; exit 1; }
[[ $EUID -eq 0 ]] || { echo "Run with sudo: sudo bash $0" >&2; exit 1; }

AGENT_USER="${SUDO_USER:-kyle}"
AGENT_HOME="$(getent passwd "$AGENT_USER" | cut -d: -f6)"
SCOPE="${SCOPE:-$AGENT_HOME/scope}"
SITE="$(cd "$(dirname "$0")/.." && pwd)"
PORT="${PORT:-8799}"
SECRETS=/etc/agent-secrets/projects/livescope
UNIT=/etc/systemd/system/livescope.service

creds=""
for name in opensky-client-id opensky-client-secret \
            swim-{tfms,sfdps}-{username,password,queue,url}; do
  f="$SECRETS/$name.secret"
  if [[ -f "$f" ]]; then
    creds+="LoadCredential=$name:$f"$'\n'
    echo "using $f"
  fi
done
[[ -n "$creds" ]] || echo "note: no OpenSky client in $SECRETS; the globe updates every 15 min instead of 90 s" >&2

# Solace's Python API for SWIM: one wheel, no dependencies, unpacked (no pip on the server) and checked by hash
PYLIB=/opt/livescope-pylib
SOLACE_VER=1.12.0
case "$(uname -m)" in
  x86_64)  WHL=solace_pubsubplus-$SOLACE_VER-py36-none-manylinux_2_12_x86_64.whl
           WHL_URL=https://files.pythonhosted.org/packages/c7/55/b7d31795fbcbec2a728a2e623bbe8ee9e53e11df7ce62f2f9df52a19c4a2/$WHL
           WHL_SHA=b8a8e1cb35db27104cb790075b8731d6b9cd7a3bc3e3448f5a6cd73d44746e18 ;;
  aarch64) WHL=solace_pubsubplus-$SOLACE_VER-py36-none-manylinux_2_17_aarch64.whl
           WHL_URL=https://files.pythonhosted.org/packages/21/2f/9d4ba4f54be24a617f66850d749dcb4dd638a12e0243f8b725875c6e081c/$WHL
           WHL_SHA=a1d6dcfd8d24b36f9727f06986b13a5bde1323a30d4eee5ebd60decf224b0db2 ;;
  *) WHL= ;;
esac
if [[ -n "$WHL" && ! -f "$PYLIB/.$WHL" ]]; then
  tmp="$(mktemp -d)"
  if curl -fsSL -o "$tmp/$WHL" "$WHL_URL" && echo "$WHL_SHA  $tmp/$WHL" | sha256sum -c --quiet -; then
    rm -rf "$PYLIB" && mkdir -p "$PYLIB"
    python3 -I -c 'import sys, zipfile; zipfile.ZipFile(sys.argv[1]).extractall(sys.argv[2])' "$tmp/$WHL" "$PYLIB"
    touch "$PYLIB/.$WHL" && chmod -R a+rX "$PYLIB"
    echo "installed solace-pubsubplus $SOLACE_VER in $PYLIB"
  else
    echo "warning: couldn't download or verify solace-pubsubplus; SWIM stays off" >&2
  fi
  rm -rf "$tmp"
fi

cat > "$UNIT" <<UNIT
[Unit]
Description=livescope (livescope.kyleboas.com)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
DynamicUser=true
${creds}EnvironmentFile=-/etc/livescope.env
Environment=SCOPE_GROUND_DIR=/var/lib/livescope/ground LIVESCOPE_TIMELAPSE_DIR=/var/lib/livescope/timelapse PYTHONPATH=$PYLIB
StateDirectory=livescope
ProtectHome=tmpfs
BindReadOnlyPaths=$SCOPE $SITE
ExecStart=/usr/bin/python3 $SCOPE/server.py $SITE --port $PORT --host 127.0.0.1
Restart=on-failure
RestartSec=5
Nice=5
UMask=0077
NoNewPrivileges=true
PrivateTmp=true
PrivateDevices=true
ProtectSystem=strict
ProtectKernelTunables=true
ProtectKernelModules=true
ProtectControlGroups=true
InaccessiblePaths=/etc/agent-secrets
CapabilityBoundingSet=
RestrictSUIDSGID=true
LockPersonality=true
RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6
MemoryMax=1G

[Install]
WantedBy=multi-user.target
UNIT
chmod 0644 "$UNIT"

# Retire the old per-user service (same port) and bring its ground-map cache and timelapses along, once.
uid="$(id -u "$AGENT_USER")"
if [[ -f "$AGENT_HOME/.config/systemd/user/livescope.service" ]]; then
  sudo -u "$AGENT_USER" XDG_RUNTIME_DIR="/run/user/$uid" systemctl --user disable --now livescope.service || true
  rm -f "$AGENT_HOME/.config/systemd/user/livescope.service"
  rm -rf "$AGENT_HOME/.config/systemd/user/livescope.service.d"
  sudo -u "$AGENT_USER" XDG_RUNTIME_DIR="/run/user/$uid" systemctl --user daemon-reload || true
fi
if [[ ! -e /var/lib/private/livescope && ! -e /var/lib/livescope ]]; then
  mkdir -p /var/lib/private/livescope && chmod 0700 /var/lib/private
  for d in ground timelapse; do
    if [[ -d "$SCOPE/livedata/$d" ]]; then cp -a "$SCOPE/livedata/$d" /var/lib/private/livescope/; echo "copied $SCOPE/livedata/$d"; fi
  done
fi

# Auto-deploy: every minute, pull scope and livescope from GitHub (as the agent user, fast-forward only)
# and restart the site when either checkout is on a new commit, so a push is all an update takes.
UPDATER=/usr/local/lib/livescope-update.sh
cat > "$UPDATER" <<SCRIPT
#!/usr/bin/env bash
set -uo pipefail
# fetch, then fast-forward to the fetched branch (not "git pull": infinitescope's updater fetches the shared
# scope checkout at the same moment, and two pulls racing over FETCH_HEAD make one fail), under one lock
exec 9>/run/lock/scope-git.lock; flock -w 50 9 || true
for repo in $SCOPE $SITE; do
  runuser -u $AGENT_USER -- git -C "\$repo" fetch -q origin || true
  runuser -u $AGENT_USER -- git -C "\$repo" merge --ff-only -q '@{u}' || echo "update failed in \$repo" >&2
done
exec 9>&-
now="\$(git -C $SCOPE rev-parse HEAD) \$(git -C $SITE rev-parse HEAD)"
last="\$(cat /var/lib/livescope-update/revs 2>/dev/null)"
if [[ "\$now" != "\$last" ]]; then
  systemctl restart livescope.service && echo "\$now" > /var/lib/livescope-update/revs && echo "deployed \$now"
fi
SCRIPT
chmod 0755 "$UPDATER"
cat > /etc/systemd/system/livescope-update.service <<UNIT
[Unit]
Description=Pull scope and livescope from GitHub and restart livescope when they change
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
Environment=GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=safe.directory GIT_CONFIG_VALUE_0=*
StateDirectory=livescope-update
ExecStart=$UPDATER
UNIT
cat > /etc/systemd/system/livescope-update.timer <<UNIT
[Unit]
Description=Check for livescope updates every minute

[Timer]
OnBootSec=2min
OnUnitActiveSec=1min

[Install]
WantedBy=timers.target
UNIT
chmod 0644 /etc/systemd/system/livescope-update.service /etc/systemd/system/livescope-update.timer

systemctl daemon-reload
systemctl enable livescope.service >/dev/null
systemctl enable --now livescope-update.timer >/dev/null
systemctl restart livescope.service
mkdir -p /var/lib/livescope-update
echo "$(git -c safe.directory='*' -C "$SCOPE" rev-parse HEAD) $(git -c safe.directory='*' -C "$SITE" rev-parse HEAD)" > /var/lib/livescope-update/revs
for _ in $(seq 1 30); do
  if curl -fsS "http://127.0.0.1:$PORT/api/live/timelapse?a=list" >/dev/null 2>&1; then
    echo "livescope is up on $PORT"
    curl -fsS "http://127.0.0.1:$PORT/api/live/timelapse?a=list" | grep -o '"world_every":[0-9]*' | sed 's/"world_every":90$/OpenSky client in use: a world picture every 90 s/; s/"world_every":900$/no OpenSky client: a world picture every 15 min/' || true
    exit 0
  fi
  sleep 1
done
echo "livescope did not answer; see: journalctl -u livescope -n 50" >&2
exit 1
