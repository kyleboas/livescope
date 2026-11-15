#!/usr/bin/env bash
# Everything that locks coding down, in one go. On coding (over Tailscale: ssh coding):
#
#   sudo bash ~/livescope/deploy/harden-coding.sh
#
# 0. Each site's own installer (livescope, vatscope, infinitescope, playscope, atc-at-home deploy/install-service.sh):
#    sandboxed system services with their own update timers. Stops at the first one that fails. Then a check that
#    the services' network block (IPAddressDeny) really works.
# 1. youtube.kyleboas.com (~/youtube-site, static files, not a repo) as a sandboxed system service: its own
#    throwaway user, the folder read-only, no network but its port.
# 2. gh-deploy (the user timer that deployed playscope and atc-at-home): turned off once nothing is left in it.
# 3. SSH over Tailscale only: port 22 is allowed on tailscale0 and no longer from the internet. Mail (25) stays.
#    If Tailscale is ever down, log in through DigitalOcean's Recovery Console (Droplet > Access), which doesn't
#    use SSH, and run: sudo ufw allow 22/tcp
#    Skipped unless Tailscale is up and this session itself came over Tailscale (or SSH_ANYWAY=1).
# 4. A reboot, if a kernel update is waiting (NO_REBOOT=1 skips it).
set -euo pipefail
[[ $EUID -eq 0 ]] || { echo "run it with sudo" >&2; exit 1; }
AGENT_USER="${SUDO_USER:-kyle}"
AGENT_HOME="$(getent passwd "$AGENT_USER" | cut -d: -f6)"
uid="$(id -u "$AGENT_USER")"
as_agent() { sudo -u "$AGENT_USER" XDG_RUNTIME_DIR="/run/user/$uid" "$@"; }
export SUDO_USER="$AGENT_USER"

# 0. the sites
for r in livescope vatscope infinitescope playscope atc-at-home; do
  inst="$AGENT_HOME/$r/deploy/install-service.sh"
  [[ -f "$inst" ]] || { echo "== $r: no installer, skipped"; continue; }
  echo "== $r"
  bash "$inst" || { echo "$r's installer failed; fix it and run this again (the ones before it are done)" >&2; exit 1; }
done
ts="$(tailscale ip -4 2>/dev/null | head -1)"
if [[ -n "$ts" ]]; then
  if systemd-run --quiet --wait --pipe -p IPAddressDeny=100.64.0.0/10 curl -so /dev/null -m3 "http://$ts:8741/" 2>/dev/null; then
    echo "WARNING: IPAddressDeny didn't block the tailnet; the sites can still reach it" >&2
  else
    echo "network block works: the sites can't reach the tailnet"
  fi
fi

# 1. youtube-site
YT="$AGENT_HOME/youtube-site"
if [[ -d "$YT" ]]; then
  if [[ -f "$AGENT_HOME/.config/systemd/user/youtube-site.service" ]]; then
    as_agent systemctl --user disable --now youtube-site.service || true
    rm -f "$AGENT_HOME/.config/systemd/user/youtube-site.service"
    as_agent systemctl --user daemon-reload || true
  fi
  cat > /etc/systemd/system/youtube-site.service <<UNIT
[Unit]
Description=youtube.kyleboas.com (home page and privacy policy)

[Service]
Type=simple
DynamicUser=true
ProtectHome=tmpfs
BindReadOnlyPaths=$YT
WorkingDirectory=$YT
ExecStart=/usr/bin/python3 -m http.server 8798 --bind 127.0.0.1 --directory $YT
Restart=always
RestartSec=5
UMask=0077
NoNewPrivileges=true
PrivateTmp=true
PrivateDevices=true
ProtectSystem=strict
ProtectKernelTunables=true
ProtectKernelModules=true
ProtectControlGroups=true
InaccessiblePaths=-/etc/agent-secrets
CapabilityBoundingSet=
RestrictSUIDSGID=true
LockPersonality=true
RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6
# static files: it never connects anywhere, so nothing but loopback (where cloudflared asks)
IPAddressDeny=any
IPAddressAllow=localhost
SystemCallArchitectures=native
SystemCallFilter=@system-service
SystemCallFilter=~@privileged
SystemCallErrorNumber=EPERM
RestrictNamespaces=true
RestrictRealtime=true
ProtectHostname=true
ProtectProc=invisible
PrivateIPC=true
MemoryMax=128M

[Install]
WantedBy=multi-user.target
UNIT
  chmod 0644 /etc/systemd/system/youtube-site.service
  systemctl daemon-reload
  systemctl enable youtube-site.service >/dev/null
  systemctl restart youtube-site.service
  sleep 2
  curl -fsS -o /dev/null http://127.0.0.1:8798/ && echo "youtube-site is up on 8798" || echo "youtube-site did not answer: journalctl -u youtube-site" >&2
fi

# 2. gh-deploy, once playscope's and atc-at-home's installers took their entries out
GHD="$AGENT_HOME/.local/bin/gh-deploy"
if [[ -f "$GHD" ]] && ! grep -qE '^ *\[[a-z-]+\]=' <(sed -n '/declare -A UNITS/,/^)/p' "$GHD"); then
  as_agent systemctl --user disable --now gh-deploy.timer && echo "gh-deploy has nothing left to deploy: timer off"
fi

# 3. SSH over Tailscale only
# where this session comes from (sudo drops SSH_CONNECTION; who -m still has the client's address)
src="$(awk '{print $1}' <<<"${SSH_CONNECTION:-}")"
[[ -n "$src" ]] || src="$(who -m 2>/dev/null | sed -n 's/.*(\(.*\)).*/\1/p')"
if ! tailscale status >/dev/null 2>&1; then
  echo "Tailscale isn't up: SSH left open" >&2
elif [[ "$src" != 100.* && "$src" != fd7a:115c:a1e0:* && "${SSH_ANYWAY:-}" != 1 ]]; then
  echo "This session didn't come over Tailscale (ssh coding via 100.72.19.59 first): SSH left open" >&2
else
  ufw allow in on tailscale0 to any port 22 proto tcp comment 'ssh over tailscale' >/dev/null
  for r in OpenSSH 22/tcp 22 ssh; do
    ufw delete allow "$r" >/dev/null 2>&1 || true
    ufw delete limit "$r" >/dev/null 2>&1 || true
  done
  if ufw status | grep -E '^22(/tcp)?( \(v6\))? +(ALLOW|LIMIT)' | grep -qv tailscale0; then
    echo "a public port 22 rule is still there; remove it by number: ufw status numbered, ufw delete N" >&2
  else
    echo "SSH: Tailscale only"
  fi
fi
ufw status verbose

# 4. reboot for a waiting kernel update
if [[ -f /var/run/reboot-required && "${NO_REBOOT:-}" != 1 ]]; then
  echo "All done. Rebooting in 15 s for the kernel update (Ctrl-C to skip); ssh coding again in a minute."
  sleep 15
  systemctl reboot
fi
echo "All done."
