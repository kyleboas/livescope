"""Capture a few minutes of an FAA SWIM feed to files, to see what its messages hold.

On the server, with the feed's secrets saved (see swim.py):
    sudo PYTHONPATH=/opt/livescope-pylib python3 tools/swim_capture.py tfms 10     # minutes
It reads the secrets straight from /etc/agent-secrets/projects/livescope/ (hence sudo), writes every message
to /tmp/swim-capture/<feed>/NNNNNN.xml.gz (owned by the user who ran sudo), and prints a count by root
element and message kind. The messages are taken off the queue, as the site would.
"""
import gzip
import os
import re
import sys
import time
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import swim  # noqa: E402

SECRETS = "/etc/agent-secrets/projects/livescope"


def secret(name):
    try:
        with open(os.path.join(SECRETS, name + ".secret"), encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        return os.environ.get(name.upper().replace("-", "_"), "").strip()


def main(feed, minutes):
    cfg = swim.config(feed, secret)
    if not cfg:
        sys.exit(f"no swim-{feed}-username / -password / -queue in {SECRETS} (sudo secret livescope swim-{feed}-username ...)")
    if not swim.available():
        sys.exit("the Solace library isn't installed: sudo bash deploy/install-service.sh, then run with PYTHONPATH=/opt/livescope-pylib")
    out = f"/tmp/swim-capture/{feed}"
    os.makedirs(out, exist_ok=True)
    kinds, n, size = Counter(), [0], [0]
    owner = (int(os.environ["SUDO_UID"]), int(os.environ["SUDO_GID"])) if "SUDO_UID" in os.environ else None

    def got(_, payload):
        n[0] += 1
        size[0] += len(payload)
        path = os.path.join(out, f"{n[0]:06d}.xml.gz")
        with gzip.open(path, "wb") as f:
            f.write(payload)
        if owner:
            os.chown(path, *owner)
        head = payload[:4000].decode("utf-8", "replace")
        root = re.search(r"<(?:[\w.-]+:)?([\w.-]+)[\s>]", re.sub(r"<\?.*?\?>", "", head))
        kind = re.search(r'\b(?:msgType|source|type)="([^"]{1,40})"', head)
        kinds[(root[1] if root else "?", kind[1] if kind else "")] += 1

    f = swim.Feed(feed, cfg, got)
    end = time.time() + minutes * 60
    while time.time() < end:
        time.sleep(10)
        print(f"{f.state}: {n[0]} messages, {size[0] / 1e6:.1f} MB" + (f" ({f.err})" if f.err else ""), flush=True)
    if owner:
        for d in ("/tmp/swim-capture", out):
            os.chown(d, *owner)
    print(f"\n{n[0]} messages in {out}")
    for (root, kind), c in kinds.most_common(40):
        print(f"{c:8d}  {root}  {kind}")
    os._exit(0)                                       # don't wait for the Solace thread


if __name__ == "__main__":
    if len(sys.argv) < 2 or sys.argv[1] not in swim.VPNS:
        sys.exit(f"usage: {sys.argv[0]} tfms|sfdps [minutes]")
    main(sys.argv[1], float(sys.argv[2]) if len(sys.argv) > 2 else 10)
