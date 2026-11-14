"""FAA SWIM flight data through SCDS (the SWIM Cloud Distribution Service): one queue per subscription,
read with Solace's Python API (solace-pubsubplus, the one outside library livescope uses; installed by
deploy/install-service.sh). SCDS allows that API without an exemption as long as compression is on.

A feed is configured by credentials (agent-secrets via deploy/install-service.sh, or the environment as
SWIM_TFMS_USERNAME etc.):
    swim-<feed>-username, swim-<feed>-password, swim-<feed>-queue    required
    swim-<feed>-url                                                  optional, default tcps://ems2.swim.faa.gov:55443
<feed> is tfms (VPN TFMS) or sfdps (VPN FDPS). A feed without them, or a server without the library, is off.
The username and queue name hold the account's e-mail address, so they're kept out of the repository too.
"""
import os
import sys
import threading
import time

URL = "tcps://ems2.swim.faa.gov:55443"
VPNS = {"tfms": "TFMS", "sfdps": "FDPS"}
TRUST = ("/etc/ssl/certs", "/tmp/livescope-certs")     # a directory of CA certificates (Solace wants a directory)


def config(feed, secret=None):
    if secret is None:
        import adsb
        secret = adsb.credential
    c = {k: secret(f"swim-{feed}-{k}") for k in ("username", "password", "queue", "url")}
    if not (c["username"] and c["password"] and c["queue"]):
        return None
    return {**c, "url": c["url"] or URL, "vpn": VPNS[feed]}


def trust_dir():
    for d in TRUST:
        if os.path.isdir(d) and any(n.endswith((".pem", ".crt")) for n in os.listdir(d)):
            return d
    if os.path.isfile("/etc/ssl/cert.pem"):           # macOS: one bundle file, no directory
        os.makedirs(TRUST[1], exist_ok=True)
        with open("/etc/ssl/cert.pem", "rb") as f, open(os.path.join(TRUST[1], "cert.pem"), "wb") as g:
            g.write(f.read())
        return TRUST[1]
    return TRUST[0]


class Feed:
    """Reads one SCDS queue forever in a background thread, reconnecting on trouble, and hands each message's
    payload (bytes, usually FIXM XML) to on_message(feed, payload). Messages are acknowledged once handled,
    so the queue doesn't grow (and SCDS doesn't disable the subscription as idle)."""

    def __init__(self, feed, cfg, on_message):
        self.feed, self.cfg, self.on_message = feed, cfg, on_message
        self.state, self.count, self.err, self.last = "starting", 0, None, 0.0
        threading.Thread(target=self.run, daemon=True, name=f"swim-{feed}").start()

    def log(self, msg):
        print(f"livescope: SWIM {self.feed}: {msg}", file=sys.stderr, flush=True)

    def run(self):
        from solace.messaging.config.retry_strategy import RetryStrategy
        from solace.messaging.config.solace_properties import authentication_properties as A
        from solace.messaging.config.solace_properties import service_properties as S
        from solace.messaging.config.solace_properties import transport_layer_properties as T
        from solace.messaging.config.transport_security_strategy import TLS
        from solace.messaging.messaging_service import MessagingService
        from solace.messaging.receiver.message_receiver import MessageHandler
        from solace.messaging.resources.queue import Queue

        feed = self

        class Handler(MessageHandler):
            def on_message(self, m):
                try:
                    payload = m.get_payload_as_bytes()
                    if payload is None:
                        s = m.get_payload_as_string()
                        payload = s.encode() if s else b""
                    feed.count += 1
                    feed.last = time.time()
                    feed.on_message(feed.feed, payload)
                except Exception as e:  # noqa: BLE001 - Solace swallows callback errors: say what went wrong
                    feed.log(f"handling a message failed: {type(e).__name__}: {e}")
                finally:
                    try:
                        receiver.ack(m)
                    except Exception:  # noqa: BLE001
                        pass

        wait = 10
        while True:
            receiver = svc = None
            try:
                props = {T.HOST: self.cfg["url"], S.VPN_NAME: self.cfg["vpn"],
                         A.SCHEME_BASIC_USER_NAME: self.cfg["username"], A.SCHEME_BASIC_PASSWORD: self.cfg["password"],
                         T.COMPRESSION_LEVEL: 1}           # SCDS requires compression
                svc = (MessagingService.builder().from_properties(props)
                       .with_reconnection_retry_strategy(RetryStrategy.forever_retry(10000))
                       .with_transport_security_strategy(TLS.create().with_certificate_validation(
                           True, validate_server_name=True, trust_store_file_path=trust_dir()))
                       .build())
                self.state = "connecting"
                svc.connect()
                receiver = svc.create_persistent_message_receiver_builder().build(
                    Queue.durable_non_exclusive_queue(self.cfg["queue"]))
                receiver.start()
                receiver.receive_async(Handler())
                self.state, self.err, wait = "connected", None, 10
                self.log(f"connected to {self.cfg['url']} ({self.cfg['vpn']})")
                while svc.is_connected:
                    time.sleep(5)
                raise ConnectionError("disconnected")
            except Exception as e:  # noqa: BLE001 - try again, backing off to 10 minutes
                self.state, self.err = "down", f"{type(e).__name__}: {str(e)[:300]}"
                self.log(f"{self.err}; retrying in {wait} s")
                try:
                    if receiver:
                        receiver.terminate()
                    if svc:
                        svc.disconnect()
                except Exception:  # noqa: BLE001
                    pass
                time.sleep(wait)
                wait = min(600, wait * 2)


def available():
    try:
        import solace.messaging  # noqa: F401
        return True
    except ImportError:
        return False
