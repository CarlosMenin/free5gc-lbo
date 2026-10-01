#!/usr/bin/env python3
"""
Verifies where the user-plane traffic of the UE leaves the 5G core.

Lower latency towards the edge application is not, by itself, evidence of a
local breakout: the same effect appears when the edge application is simply
closer while the session remains anchored in the core. The verification is
therefore a direct observation of the user plane. Traffic is sent from inside
the PDU session to the edge application and then to the remote application,
and the packet counters of the GTP-U interface of each UPF are read before and
after each run.

    Scenario B   the PSA-UPF must NOT count the traffic sent to the edge
                 application, and must count the traffic sent to the remote one.
    Scenario A   the central UPF must count both.

The scenario is detected from the UPF Deployments that exist in the cluster.
See docs/09-verification.md.

Usage:
    python3 scripts/verify-lbo.py
    python3 scripts/verify-lbo.py --count 2000 --interval-ms 5
"""
import argparse
import json
import re
import subprocess
import sys
import time

NS = "free5gc"
GTP_IF = "upfgtp"              # interface created by gtp5g inside the UPF pod
PROBE_PORT = 9000
EDGE_SERVICE = "mec-app"
REMOTE_SERVICE = "remote-app"
# Share of the remote-path packet count tolerated at the PSA-UPF during the
# edge run before the breakout is declared inactive.
LEAK_THRESHOLD = 0.02

GREEN, RED, YELLOW, GREY, RESET = "\033[32m", "\033[31m", "\033[33m", "\033[90m", "\033[0m"
if not sys.stdout.isatty():
    GREEN = RED = YELLOW = GREY = RESET = ""


class Failure(Exception):
    """Stops the verification; the message is already written for the reader."""


def sh(cmd, timeout=120):
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    return p.returncode, p.stdout, p.stderr


def kubectl(*args, timeout=120):
    return sh(["kubectl", "-n", NS, *args], timeout=timeout)


def step(n, text):
    print(f"\n[{n}] {text}")


def ok(text):
    print(f"    {GREEN}ok{RESET}  {text}")


def note(text):
    print(f"    {GREY}{text}{RESET}")


def wait_for(description, condition, timeout, interval=3):
    """Calls condition() until it returns a truthy value or the timeout expires."""
    t0 = time.time()
    while time.time() - t0 < timeout:
        value = condition()
        if value:
            return value
        time.sleep(interval)
    raise Failure(f"timeout waiting for {description} ({timeout}s)")


def deployment_exists(name):
    return kubectl("get", "deploy", name, "-o", "name")[0] == 0


def detect_scenario():
    has_a = deployment_exists("free5gc-upf")
    has_b = deployment_exists("free5gc-i-upf") and deployment_exists("free5gc-psa-upf")
    if has_a and has_b:
        raise Failure("the user planes of both scenarios are deployed at the same time; "
                      "redeploy one of them with scripts/deploy.py")
    if has_b:
        return "b", {"i-upf": "free5gc-i-upf", "psa-upf": "free5gc-psa-upf"}
    if has_a:
        return "a", {"upf": "free5gc-upf"}
    raise Failure("no UPF Deployment found in the namespace; deploy a scenario first")


def cluster_ip(service):
    rc, out, err = kubectl("get", "svc", service, "-o", "jsonpath={.spec.clusterIP}")
    ip = out.strip()
    if rc != 0 or not ip or ip == "None":
        raise Failure(f"could not read the ClusterIP of Service {service}: {err.strip()}")
    return ip


def gtp_counters(deploy):
    """RX/TX packet counters of the gtp5g interface inside the UPF pod."""
    rc, out, err = kubectl("exec", f"deploy/{deploy}", "--", "cat", "/proc/net/dev", timeout=60)
    if rc != 0:
        raise Failure(f"could not read /proc/net/dev in {deploy}: {err.strip()}")
    for line in out.splitlines():
        m = re.match(r"\s*" + GTP_IF + r":\s*(.*)$", line)
        if m:
            fields = m.group(1).split()
            if len(fields) >= 10:
                return {"rx": int(fields[1]), "tx": int(fields[9])}
    raise Failure(f"interface '{GTP_IF}' does not exist in {deploy}: gtp5g did not create "
                  "the data path. Check that the module is loaded on the node "
                  "(docs/02, section 2.3)")


def af_subscriptions():
    """Subscriptions currently held by the Application Function, or None."""
    code = ("import urllib.request as u;"
            "print(u.urlopen('http://127.0.0.1:8080/status', timeout=5).read().decode())")
    rc, out, _ = kubectl("exec", "deploy/mec-af", "--", "python3", "-c", code, timeout=60)
    if rc != 0:
        return None
    try:
        return json.loads(out).get("subscriptions", {})
    except ValueError:
        return None


def run_probe(args, target_ip, ue_ip, label):
    duration = args.count * args.interval_ms / 1000.0
    rc, out, err = kubectl(
        "exec", args.pod, "-c", "probe", "--", "python3", "/opt/probe/probe.py", "client",
        "--server", f"{target_ip}:{PROBE_PORT}", "--bind", ue_ip,
        "--count", str(args.count), "--interval-ms", str(args.interval_ms),
        "--label", label, timeout=int(duration) + 90)
    try:
        return json.loads(out[out.index("{"):out.rindex("}") + 1])
    except ValueError:
        raise Failure(f"the probe returned no summary for {target_ip} (rc={rc}).\n"
                      f"    stdout: {out.strip()[:300]}\n    stderr: {err.strip()[:300]}")


def measure(args, upfs, target_ip, ue_ip, label):
    before = {name: gtp_counters(deploy) for name, deploy in upfs.items()}
    note(f"sending {args.count} datagrams to {target_ip} ({label})")
    summary = run_probe(args, target_ip, ue_ip, label)
    after = {name: gtp_counters(deploy) for name, deploy in upfs.items()}
    delta = {name: {k: after[name][k] - before[name][k] for k in ("rx", "tx")}
             for name in upfs}
    if not summary["received"]:
        raise Failure(
            f"no echo came back from {target_ip}: the data path is not working.\n"
            "    Suspects, in this order (docs/10-troubleshooting.md):\n"
            "      (a) the route of the destination through the tunnel interface;\n"
            "      (b) ip_forward=0 in the UPF container;\n"
            "      (c) gtp5g not loaded on the node of a UPF;\n"
            "      (d) the target pod is not ready.")
    return summary, delta


def verify(args):
    step(1, "Scenario")
    scenario, upfs = detect_scenario()
    ok(f"scenario {scenario.upper()} ({', '.join(upfs.values())})")

    step(2, "UE registration and PDU session")
    def registered():
        rc, out, _ = kubectl("exec", args.pod, "-c", "ue", "--",
                             "./nr-cli", args.imsi, "-e", "status", timeout=30)
        return rc == 0 and "RM-REGISTERED" in out
    try:
        wait_for("the UE registration", registered, args.timeout)
    except Failure:
        raise Failure(
            "the UE is not registered.\n"
            "    The UE only reports a generic cause; inspect the UDM and AMF logs.\n"
            "    When UERANSIM was started before the subscribers existed, the UE has\n"
            "    given up: delete the pod so that it registers again\n"
            f"    (kubectl -n {NS} delete pod {args.pod}).")
    ok(f"{args.imsi} is RM-REGISTERED")

    def tunnel_address():
        rc, out, _ = kubectl("exec", args.pod, "-c", "ue", "--",
                             "ip", "-4", "-o", "addr", "show", args.tun, timeout=30)
        m = re.search(r"inet (\d+\.\d+\.\d+\.\d+)", out) if rc == 0 else None
        return m.group(1) if m else None
    ue_ip = wait_for(f"an address on {args.tun}", tunnel_address, args.timeout)
    ok(f"{args.tun} = {ue_ip} (allocated by the anchor UPF)")

    step(3, "Destinations")
    edge_ip, remote_ip = cluster_ip(EDGE_SERVICE), cluster_ip(REMOTE_SERVICE)
    ok(f"{EDGE_SERVICE} = {edge_ip}, {REMOTE_SERVICE} = {remote_ip}")

    if scenario == "b":
        subs = af_subscriptions()
        if subs is None:
            print(f"    {YELLOW}warning{RESET}  could not read the Application Function status; "
                  "a static rule may be in use")
        else:
            announced = sorted({v.get("ip") for v in subs.values()})
            if edge_ip in announced:
                ok(f"the Application Function holds {len(subs)} subscription(s) for {edge_ip}")
            else:
                print(f"    {YELLOW}warning{RESET}  the Application Function has not announced "
                      f"{edge_ip} (announced: {announced or 'nothing'})")

    step(4, "Routes through the PDU session")
    # Without these routes the traffic leaves through the pod interface,
    # reaches the destination over the cluster network and crosses no UPF.
    for dest in (edge_ip, remote_ip):
        rc, _, err = kubectl("exec", args.pod, "-c", "ue", "--",
                             "ip", "route", "replace", f"{dest}/32", "dev", args.tun, timeout=30)
        if rc != 0:
            raise Failure(f"could not install the route to {dest}: {err.strip()}")
    ok(f"{edge_ip} and {remote_ip} routed through {args.tun}")

    step(5, "Traffic and UPF counters")
    edge, d_edge = measure(args, upfs, edge_ip, ue_ip, "edge")
    remote, d_remote = measure(args, upfs, remote_ip, ue_ip, "remote")

    print(f"\n    Packets on the GTP-U interface of each UPF during each run "
          f"(rx = uplink, tx = downlink):\n")
    header = f"    {'destination':<14}" + "".join(f"{name + ' rx':>14}{name + ' tx':>14}"
                                                  for name in upfs)
    print(header)
    for label, delta in (("edge", d_edge), ("remote", d_remote)):
        print(f"    {label:<14}" + "".join(f"{delta[n]['rx']:>14}{delta[n]['tx']:>14}"
                                           for n in upfs))

    print(f"\n    {'destination':<14}{'sent':>8}{'received':>10}{'loss %':>9}"
          f"{'rtt p50 ms':>12}{'rtt p95 ms':>12}")
    for label, s in (("edge", edge), ("remote", remote)):
        print(f"    {label:<14}{s['sent']:>8}{s['received']:>10}{s['loss_pct']:>9.2f}"
              f"{s['rtt_ms']['p50']:>12.3f}{s['rtt_ms']['p95']:>12.3f}")
    print(f"    {GREY}(round-trip times are indicative; see docs/09-verification.md){RESET}")

    step(6, "Verdict")
    if scenario == "b":
        psa_edge, psa_remote = d_edge["psa-upf"]["rx"], d_remote["psa-upf"]["rx"]
        if d_edge["i-upf"]["rx"] == 0:
            raise Failure("the I-UPF did not count the edge traffic: the tunnel does not "
                          "terminate there. Check the N3 endpoint delivered to the gNB.")
        if psa_remote == 0:
            raise Failure(
                "the PSA-UPF did not count the traffic sent to the remote application: the "
                "default path (N9 up to the anchor) is not working.\n"
                "    Check the I-UPF -> PSA-UPF link in 'links' and the topology of the "
                "UE routing file.")
        leak = psa_edge / psa_remote
        if leak >= LEAK_THRESHOLD:
            raise Failure(
                f"LOCAL BREAKOUT NOT ACTIVE: the PSA-UPF counted {psa_edge} packets during "
                f"the edge run ({leak * 100:.1f}% of the remote run).\n"
                "    The edge traffic is reaching the anchor. Suspects:\n"
                "      (a) 'ulcl: true' missing in the SMF configuration;\n"
                "      (b) the IMSI of the UE is not listed in ueRoutingInfo.members;\n"
                f"      (c) the Application Function has not announced {edge_ip}\n"
                f"          (kubectl -n {NS} logs deploy/mec-af);\n"
                "      (d) the NEF did not write to the UDR, or the PCF did not notify the\n"
                "          SMF (search for 'influence' in the NEF, PCF and SMF logs).")
        ok(f"{GREEN}LOCAL BREAKOUT CONFIRMED{RESET}: the PSA-UPF counted {psa_edge} packets "
           f"during the edge run and {psa_remote} during the remote run")
    else:
        upf_edge, upf_remote = d_edge["upf"]["rx"], d_remote["upf"]["rx"]
        if upf_remote == 0 or upf_edge < 0.5 * upf_remote:
            raise Failure(
                f"the central UPF counted {upf_edge} packets during the edge run and "
                f"{upf_remote} during the remote run; in scenario A both flows must be "
                "anchored there.\n    Check that no user plane of scenario B is still deployed.")
        ok(f"{GREEN}CENTRAL ANCHORING CONFIRMED{RESET}: the central UPF counted both flows "
           f"({upf_edge} and {upf_remote} packets)")


def main():
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--pod", default="free5gc-ueransim-0", help="UERANSIM pod")
    parser.add_argument("--tun", default="uesimtun0", help="tunnel interface of the UE")
    parser.add_argument("--imsi", default="imsi-208930000000000", help="IMSI of the UE")
    parser.add_argument("--count", type=int, default=500,
                        help="datagrams per destination (default: 500)")
    parser.add_argument("--interval-ms", type=float, default=10.0,
                        help="interval between datagrams, in ms (default: 10)")
    parser.add_argument("--timeout", type=int, default=120,
                        help="seconds to wait for the registration and the session (default: 120)")
    args = parser.parse_args()

    try:
        verify(args)
    except Failure as exc:
        print(f"\n{RED}FAILED:{RESET} {exc}")
        return 1
    except subprocess.TimeoutExpired as exc:
        print(f"\n{RED}FAILED:{RESET} command timed out: {' '.join(exc.cmd[:6])} ...")
        return 1
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
