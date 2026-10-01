#!/usr/bin/env python3
"""
mec-af -- Application Function that announces edge services to the 5G core.

For every service listed in SERVICES, the AF resolves the service name through
DNS and keeps one Traffic Influence subscription per S-NSSAI at the NEF
(TS 29.522, /3gpp-traffic-influence/v1). The core propagates the rule:

    AF --POST/PUT/DELETE--> NEF --influenceData--> UDR --notification--> PCF
    PCF --SmPolicyUpdateNotify--> SMF --PFCP Session Modification--> I-UPF

The design and the behaviours of the free5GC NEF that shaped it are described
in docs/06-traffic-influence.md.

Standard library only; runs on a stock python:3.12-slim image.

Environment variables:
    NS          namespace used to build the service FQDN      free5gc
    SERVICES    edge services, comma separated                mec-app
                (a short name becomes <name>.<NS>.svc.cluster.local; an FQDN
                 or a literal IPv4 address is used as is)
    NEF_URL     base URL of the NEF          http://nef.free5gc.svc.cluster.local:8000
    UDR_URL     base URL of the UDR          http://udr.free5gc.svc.cluster.local:8000
                (used only to remove orphan records; the /nudr-dr/v2 or /v1
                 prefix is detected on the first call)
    AF_ID       AF identifier in the NEF URL                  af001
    DNN         DNN of the target sessions                    internet
    SNSSAIS     comma-separated list of sst:sd                1:010203,1:112233
    UE_CIDR     UE address pool ("to" side of the filter)     60.60.0.0/16
    DNAI        DNAI of the edge domain (SMF configuration)   mec
    NOTIFY_URL  destination of the NEF notifications
                             http://mec-af.free5gc.svc.cluster.local:8080/notify
    POLL_S      DNS polling interval, in seconds              10
    HTTP_PORT   port of the notification/status server        8080
"""
import json
import os
import re
import signal
import socket
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def env(name, default):
    v = os.environ.get(name)
    return v if v not in (None, "") else default


CFG = {
    "ns": env("NS", "free5gc"),
    "services": env("SERVICES", "mec-app"),
    "nef_url": env("NEF_URL", "http://nef.free5gc.svc.cluster.local:8000").rstrip("/"),
    "udr_url": env("UDR_URL", "http://udr.free5gc.svc.cluster.local:8000").rstrip("/"),
    "af_id": env("AF_ID", "af001"),
    "dnn": env("DNN", "internet"),
    "snssais": env("SNSSAIS", "1:010203,1:112233"),
    "ue_cidr": env("UE_CIDR", "60.60.0.0/16"),
    "dnai": env("DNAI", "mec"),
    "notify_url": env("NOTIFY_URL", "http://mec-af.free5gc.svc.cluster.local:8080/notify"),
    "poll_s": int(env("POLL_S", "10")),
    "http_port": int(env("HTTP_PORT", "8080")),
}


def log(msg):
    print(time.strftime("%Y-%m-%d %H:%M:%S"), msg, flush=True)


def parse_snssais(spec):
    out = []
    for item in spec.split(","):
        item = item.strip()
        if not item:
            continue
        sst, _, sd = item.partition(":")
        s = {"sst": int(sst)}
        if sd:
            s["sd"] = sd
        out.append((item, s))
    return out


def parse_services(spec):
    """Short name -> Service FQDN. FQDNs and literal addresses are kept."""
    out = []
    for item in spec.split(","):
        item = item.strip()
        if not item:
            continue
        fqdn = item if ("." in item) else f"{item}.{CFG['ns']}.svc.cluster.local"
        out.append((item.split(".")[0], fqdn))
    return out


SNSSAIS = parse_snssais(CFG["snssais"])
SERVICES = parse_services(CFG["services"])


def snssai_key(s):
    s = s or {}
    return f"{s.get('sst')}:{s.get('sd', '')}".rstrip(":")


def resolve(fqdn):
    """Current ClusterIP of the Service, or None when the name does not resolve.

    A name that does not resolve means the Service does not exist (yet), which
    is the signal to withdraw the corresponding subscription from the NEF."""
    try:
        return socket.gethostbyname(fqdn)
    except socket.gaierror:
        return None


def http(method, url, body=None, timeout=15):
    """Returns (status, headers, body) without raising on 4xx/5xx."""
    data = json.dumps(body).encode() if body is not None else None
    h = {"Accept": "application/json"}
    if data is not None:
        h["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, method=method, headers=h)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
            try:
                parsed = json.loads(raw) if raw else None
            except ValueError:
                parsed = raw.decode(errors="replace")
            return r.status, dict(r.headers), parsed
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read().decode(errors="replace")


# ------------------------------------------------------------------ NEF
class Nef:
    """Client of the Nnef_TrafficInfluence API (TS 29.522, clause 5.4)."""

    def __init__(self):
        self.url = f"{CFG['nef_url']}/3gpp-traffic-influence/v1/{CFG['af_id']}/subscriptions"

    @staticmethod
    def sub_id(headers, body):
        loc = headers.get("Location")
        if not loc and isinstance(body, dict):
            loc = body.get("self")
        return loc.rstrip("/").rsplit("/", 1)[-1] if loc else None

    def alive(self):
        """Any HTTP response means the NEF is up, including 404.

        The AF only exists at the NEF after its first subscription is created;
        until then the list request answers 404 "AF is not found". Requiring
        200 here would wait for a condition that only the POST itself creates."""
        try:
            http("GET", self.url)
            return True
        except OSError as e:      # connection refused, DNS failure, timeout
            log(f"NEF not answering yet: {type(e).__name__}: {e}")
            return False

    def list(self):
        """(status, list). 404 means the AF does not exist yet, which is normal
        before the first POST and equivalent to an empty list."""
        st, _, body = http("GET", self.url)
        return st, (body if isinstance(body, list) else [])

    def create(self, body):
        return http("POST", self.url, body)

    def update(self, sub_id, body):
        return http("PUT", f"{self.url}/{sub_id}", body)

    def delete(self, sub_id):
        return http("DELETE", f"{self.url}/{sub_id}")


# ------------------------------------------------------------------ UDR (clean-up only)
class Udr:
    """Direct access to the UDR, used only to delete orphan records left behind
    by a restarted NEF. Nothing is ever created or modified here."""

    def __init__(self):
        self.url = None     # resolved on the first call: v2 (current) or v1

    def _resolve(self):
        if self.url:
            return True
        for ver in ("v2", "v1"):
            url = f"{CFG['udr_url']}/nudr-dr/{ver}/application-data/influenceData"
            # Status 200 is sufficient: for an empty result the UDR answers 200
            # with the body "null" (not "[]").
            if http("GET", f"{url}?dnns={urllib.parse.quote(CFG['dnn'])}")[0] == 200:
                self.url = url
                log(f"UDR: influenceData at {url}")
                return True
        log("UDR: no /nudr-dr version answered 200; skipping the sweep this time")
        return False

    def list_ours(self):
        """influenceData records written on behalf of this AF: (id, ip, snssai)."""
        if not self._resolve():
            return None
        st, _, body = http("GET", f"{self.url}?dnns={urllib.parse.quote(CFG['dnn'])}")
        if st != 200:
            log(f"UDR GET influenceData -> {st}; skipping the sweep")
            return None
        body = body or []       # body "null" = no record
        ours = []
        for it in body:
            if not isinstance(it, dict) or it.get("interGroupId") != "AnyUE":
                continue
            if it.get("dnn") != CFG["dnn"]:
                continue
            routes = it.get("trafficRoutes") or []
            if not routes or routes[0].get("dnai") != CFG["dnai"]:
                continue
            ip = None
            for tf in it.get("trafficFilters") or []:
                for fd in tf.get("flowDescriptions") or []:
                    m = FLOW_RE.match(fd)
                    if m:
                        ip = m.group(1)
            res = it.get("resUri") or ""
            iid = res.rstrip("/").rsplit("/", 1)[-1] if res else None
            if ip and iid:
                ours.append((iid, ip, snssai_key(it.get("snssai"))))
        return ours

    def delete(self, iid):
        return http("DELETE", f"{self.url}/{iid}")[0]


# ------------------------------------------------------------------ rule
def flow_description(ip):
    # Format of the free5GC Traffic Influence guide. "out" is the downlink
    # direction (server -> UE); the UPF applies the filter in both directions.
    return f"permit out ip from {ip}/32 to {CFG['ue_cidr']}"


FLOW_RE = re.compile(
    r"^permit out ip from (\d+\.\d+\.\d+\.\d+)/32 to " + re.escape(CFG["ue_cidr"]) + r"$")


def subscription_body(svc, ip, snssai):
    return {
        "afServiceId": svc,
        "dnn": CFG["dnn"],
        "snssai": snssai,
        "anyUeInd": True,
        "notificationDestination": CFG["notify_url"],
        "trafficFilters": [{"flowId": 1, "flowDescriptions": [flow_description(ip)]}],
        "trafficRoutes": [{"dnai": CFG["dnai"]}],
    }


# ------------------------------------------------------------------ reconciliation
class Reconciler:
    """Keeps (service x S-NSSAI) -> NEF subscription carrying the current address."""

    def __init__(self, nef, udr):
        self.nef, self.udr = nef, udr
        self.subs = {}          # (svc, snssai_key) -> {"id": str, "ip": str}
        self.lock = threading.Lock()

    def snapshot(self):
        with self.lock:
            return {f"{k[0]}/{k[1]}": dict(v) for k, v in self.subs.items()}

    # --- adoption: rebuilds the local state from the NEF itself -------
    def adopt(self):
        """Reads the subscriptions under this AF_ID and takes them over.

        This makes a restart of the AF idempotent: when the right subscription
        already exists, nothing is created. The AF_ID is used exclusively by
        this controller, so anything under it that does not correspond to a
        service in SERVICES is a leftover of a previous run and is removed."""
        st, subs = self.nef.list()
        if st == 404:
            self.subs = {}          # the AF does not exist yet: nothing to adopt
            return True
        if st != 200:
            log(f"NEF GET subscriptions -> {st}; keeping the current state")
            return False
        wanted = {n for n, _ in SERVICES}
        adopted, stale = {}, []
        for s in subs:
            sid = self.nef.sub_id({}, s)
            svc = s.get("afServiceId")
            ip = None
            for tf in s.get("trafficFilters") or []:
                for fd in tf.get("flowDescriptions") or []:
                    m = FLOW_RE.match(fd)
                    if m:
                        ip = m.group(1)
            key = (svc, snssai_key(s.get("snssai")))
            if not sid or svc not in wanted or ip is None or key in adopted:
                stale.append(sid)
                continue
            adopted[key] = {"id": sid, "ip": ip}
        for sid in stale:
            if sid:
                log(f"NEF: deleting stale subscription {sid} (leftover of a "
                    f"previous run) -> {self.nef.delete(sid)[0]}")
        if adopted != self.subs:
            log(f"adopted {len(adopted)} subscription(s) from the NEF: "
                + (", ".join(f"{k[0]}[{k[1]}]=sub {v['id']}@{v['ip']}"
                             for k, v in sorted(adopted.items())) or "none"))
        self.subs = adopted
        return True

    # --- UDR: delete whatever does not belong to a live subscription --
    def sweep_udr(self):
        ours = self.udr.list_ours()
        if ours is None:
            return
        live = {(v["ip"], k[1]) for k, v in self.subs.items()}
        # A live subscription has exactly ONE record; any repetition (same
        # address and slice) is an orphan of a previous NEF. Only the last one
        # seen is kept, which is the one the current NEF wrote last.
        seen = set()
        for iid, ip, sk in reversed(ours):
            key = (ip, sk)
            if key in live and key not in seen:
                seen.add(key)
                continue
            st = self.udr.delete(iid)
            log(f"UDR: deleted orphan influenceData {iid} ({ip} [{sk}]) -> {st}")

    # --- ensure / remove ----------------------------------------------
    def ensure(self, svc, ip):
        for sk, snssai in SNSSAIS:
            key = (svc, sk)
            cur = self.subs.get(key)
            if cur and cur["ip"] == ip:
                continue
            body = subscription_body(svc, ip, snssai)
            if cur:
                st, _, resp = self.nef.update(cur["id"], body)
                if st in (200, 204):
                    log(f"{svc} [{sk}] {cur['ip']} -> {ip}: PUT ok (sub {cur['id']})")
                    cur["ip"] = ip
                    continue
                log(f"{svc} [{sk}] PUT sub {cur['id']} -> {st}: {resp}; recreating")
                self.subs.pop(key, None)
            st, hdr, resp = self.nef.create(body)
            sid = self.nef.sub_id(hdr, resp) if st == 201 else None
            if sid:
                self.subs[key] = {"id": sid, "ip": ip}
                log(f"{svc} [{sk}] -> {ip}: POST ok (sub {sid}); "
                    f"SDF='{flow_description(ip)}' dnai={CFG['dnai']}")
            else:
                log(f"{svc} [{sk}] POST -> {st}: {resp}")

    def remove(self, svc):
        for key in [k for k in self.subs if k[0] == svc]:
            cur = self.subs.pop(key)
            st, _, _ = self.nef.delete(cur["id"])
            log(f"{svc} [{key[1]}] removed: DELETE sub {cur['id']} -> {st}")

    def tick(self, desired):
        """desired: {short_name: ip}. Called at every DNS poll."""
        with self.lock:
            # Adoption also covers a restarted NEF: the list comes back empty
            # (or 404), the local state is cleared and everything is recreated.
            if not self.adopt():
                return
            missing = {(s, sk) for s in desired for sk, _ in SNSSAIS} - set(self.subs)
            for svc in {k[0] for k in self.subs} - set(desired):
                self.remove(svc)
            if missing:
                # The UDR is swept BEFORE creating: an old record for the same
                # address would become a duplicate PCC rule in every session.
                # Only when something has to be created, to avoid polling the
                # UDR at every cycle.
                self.sweep_udr()
            for svc, ip in desired.items():
                self.ensure(svc, ip)

    # There is intentionally no shutdown(): see the comment in term().


# ------------------------------------------------------------------ HTTP (notifications + status)
def http_server(rec):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, code, body=b"", ctype="application/json"):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if body:
                self.wfile.write(body)

        def do_GET(self):
            if self.path.startswith("/healthz"):
                self._send(200, b"ok", "text/plain")
            elif self.path.startswith("/status"):
                self._send(200, json.dumps(
                    {"config": CFG, "subscriptions": rec.snapshot()}, indent=2).encode())
            else:
                self._send(404)

        def do_POST(self):
            # UpPathChange and similar events (TS 29.522 EventNotification).
            # They are only logged.
            n = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(n) if n else b""
            log(f"NEF notification at {self.path}: {raw.decode(errors='replace')[:500]}")
            self._send(204)

    srv = ThreadingHTTPServer(("0.0.0.0", CFG["http_port"]), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    log(f"notification/status HTTP server on :{CFG['http_port']} (/notify, /status, /healthz)")


# ------------------------------------------------------------------ main
def main():
    log("mec-af starting with " + json.dumps(CFG))
    if not SNSSAIS:
        sys.exit("SNSSAIS is empty")
    if not SERVICES:
        sys.exit("SERVICES is empty")
    log("watched services: " + ", ".join(f"{n} -> {f}" for n, f in SERVICES))

    nef, udr = Nef(), Udr()
    rec = Reconciler(nef, udr)
    http_server(rec)

    def term(signum, frame):
        # Subscriptions are NOT deleted on exit, for two reasons:
        #  1. during a rollout the old and the new pod may overlap; the new one
        #     has already adopted the subscriptions when the old one receives
        #     SIGTERM, and deleting here would remove what was just adopted;
        #  2. the AF is only the announcer. The rule lives in the core, and a
        #     restart of the announcer must not withdraw the breakout from PDU
        #     sessions that are in use.
        # Leftovers are collected by the adoption and by the UDR sweep on the
        # next start.
        log("SIGTERM: exiting without touching the subscriptions")
        os._exit(0)
    signal.signal(signal.SIGTERM, term)
    signal.signal(signal.SIGINT, term)

    while not nef.alive():
        log(f"waiting for the NEF at {nef.url}")
        time.sleep(5)
    log(f"NEF reachable at {nef.url}")

    previous = None
    while True:
        try:
            desired = {}
            for name, fqdn in SERVICES:
                ip = resolve(fqdn)
                if ip:
                    desired[name] = ip
            if desired != previous:
                log("DNS: " + (", ".join(f"{n}={ip}" for n, ip in sorted(desired.items()))
                               or "no service resolves"))
                previous = desired
            rec.tick(desired)
        except Exception as e:            # noqa: BLE001 -- control loop
            log(f"error in the control loop: {type(e).__name__}: {e}")
        time.sleep(CFG["poll_s"])


if __name__ == "__main__":
    main()
