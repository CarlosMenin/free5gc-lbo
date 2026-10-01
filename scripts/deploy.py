#!/usr/bin/env python3
"""
Deploys or removes the free5GC v4.2.3 testbed in one of the two scenarios.

    python3 scripts/deploy.py up a     # scenario A: centralized UPF (baseline)
    python3 scripts/deploy.py up b     # scenario B: edge UPF with ULCL / local breakout
    python3 scripts/deploy.py ran      # UERANSIM (only after the subscribers exist)
    python3 scripts/deploy.py status   # quick view of the current state
    python3 scripts/deploy.py down     # removes every resource of the tutorial

The network functions are applied one at a time, in dependency order, and the
next one is applied only after the previous one has confirmed its registration
at the NRF. The reason is described in docs/03-control-plane.md, section 3.4:
a function that starts before the NRF is able to answer keeps running with an
empty instance identifier, and the failure only surfaces later, during the UE
registration.
"""
import argparse
import glob
import os
import re
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
NS = "free5gc"
M = "manifests"

# Dependency order of the control plane:
#   mongo  <- NRF (stores NF profiles) and web console (subscribers)
#   NRF    <- every other function
#   UDR    <- UDM;  UDM <- AUSF/AMF/SMF;  NSSF/PCF <- AMF/SMF
#   NEF    after UDR and PCF; only the Application Function depends on it
#   AMF    last among the signalling functions
COMMON = [
    (f"{M}/common/mongo.yaml", "free5gc-mongodb"),
    (f"{M}/common/nrf.yaml", "free5gc-nrf"),
    (f"{M}/common/udr.yaml", "free5gc-udr"),
    (f"{M}/common/udm.yaml", "free5gc-udm"),
    (f"{M}/common/ausf.yaml", "free5gc-ausf"),
    (f"{M}/common/nssf.yaml", "free5gc-nssf"),
    (f"{M}/common/pcf.yaml", "free5gc-pcf"),
    (f"{M}/common/nef.yaml", "free5gc-nef"),
    (f"{M}/common/amf.yaml", "free5gc-amf"),
    (f"{M}/common/webui.yaml", "free5gc-webui"),
]

# UPFs before the SMF: the SMF initiates the PFCP association and finds the
# UPFs already running.
SCENARIOS = {
    "a": [(f"{M}/scenario-a-central-upf/upf.yaml", "free5gc-upf"),
          (f"{M}/scenario-a-central-upf/smf.yaml", "free5gc-smf")],
    "b": [(f"{M}/scenario-b-edge-upf/i-upf.yaml", "free5gc-i-upf"),
          (f"{M}/scenario-b-edge-upf/psa-upf.yaml", "free5gc-psa-upf"),
          (f"{M}/scenario-b-edge-upf/smf.yaml", "free5gc-smf")],
}

# apps.yaml is listed twice because it holds two Deployments and each entry
# waits for one of them; applying it twice is idempotent.
APPS = [
    (f"{M}/apps/code-configmaps.yaml", None),
    (f"{M}/apps/apps.yaml", "free5gc-mec-app"),
    (f"{M}/apps/apps.yaml", "free5gc-remote-app"),
    (f"{M}/apps/mec-af.yaml", "mec-af"),
]

RAN = f"{M}/ran/ueransim.yaml"

# Functions with a service-based interface, which register at the NRF. UPFs
# are not included: they only speak PFCP with the SMF.
REGISTER_AT_NRF = {
    "free5gc-udr", "free5gc-udm", "free5gc-ausf", "free5gc-nssf",
    "free5gc-pcf", "free5gc-nef", "free5gc-amf", "free5gc-smf", "free5gc-webui",
}

# Pods that read configuration or code from a ConfigMap only at start-up.
RESTART_ON_APPLY = {"free5gc-smf", "mec-af"}

MARK_OK = "OAuth2 setting receive from NRF"
MARK_ERROR = "register to NRF Error"
NRF_READY = "SBI server started"
PLACEHOLDER = "CHANGE-ME"

GREEN, RED, YELLOW, GREY, RESET = "\033[32m", "\033[31m", "\033[33m", "\033[90m", "\033[0m"
if not sys.stdout.isatty():
    GREEN = RED = YELLOW = GREY = RESET = ""


def run(cmd, check=True, quiet=False, timeout=180):
    if not quiet:
        print(f"  {GREY}$ {cmd}{RESET}")
    r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)
    if r.stdout.strip() and not quiet:
        print("    " + r.stdout.strip().replace("\n", "\n    "))
    if r.returncode != 0 and check and not quiet:
        print(f"    {RED}ERROR:{RESET} {r.stderr.strip()}", file=sys.stderr)
    return r


def strip_ansi(s):
    return re.sub(r"\x1b\[[0-9;]*m", "", s)


def check_placeholders():
    """Refuses to deploy while a manifest still carries a placeholder hostname."""
    pending = []
    for path in sorted(glob.glob(f"{M}/**/*.yaml", recursive=True)):
        with open(path) as fh:
            if any(PLACEHOLDER in line and not line.strip().startswith("#") for line in fh):
                pending.append(path)
    if pending:
        print(f"{RED}Node placeholders ({PLACEHOLDER}-*) are still present in:{RESET}")
        for path in pending:
            print(f"    {path}")
        print("Replace them with the hostnames of the cluster nodes "
              "(docs/02-testbed-and-prerequisites.md, section 2.4).")
        sys.exit(1)


def ensure_namespace():
    if run(f"kubectl get namespace {NS}", check=False, quiet=True).returncode != 0:
        print(f"==> Creating namespace {NS}")
        if run(f"kubectl apply -f {M}/namespace.yaml").returncode != 0:
            sys.exit(1)


def wait_rollout(deploy, timeout):
    r = run(f"kubectl -n {NS} rollout status deploy/{deploy} --timeout={timeout}s",
            check=False, quiet=True, timeout=timeout + 30)
    return r.returncode == 0


def registered_at_nrf(deploy):
    """True when the function confirmed its registration AFTER its last
    registration error.

    The position matters: a function that failed, retried and succeeded is
    healthy; one that only failed is not. The presence of the two markers
    alone does not tell these cases apart."""
    r = run(f"kubectl -n {NS} logs deploy/{deploy} --tail=400",
            check=False, quiet=True, timeout=60)
    if r.returncode != 0:
        return None
    last_ok = last_error = -1
    for i, line in enumerate(strip_ansi(r.stdout).splitlines()):
        if MARK_OK in line:
            last_ok = i
        elif MARK_ERROR in line:
            last_error = i
    return last_ok > last_error


def wait_registration(deploy, timeout):
    t0 = time.time()
    while time.time() - t0 < timeout:
        if registered_at_nrf(deploy):
            return True
        time.sleep(3)
    return False


def bring_up(manifest, deploy, args):
    print(f"\n==> {manifest}")
    if run(f"kubectl apply -f {manifest}").returncode != 0:
        sys.exit(1)
    if not deploy:
        return

    if deploy in RESTART_ON_APPLY:
        # When only the ConfigMap changed, "kubectl apply" does not recreate
        # the pod and the process keeps the previous configuration.
        run(f"kubectl -n {NS} rollout restart deploy/{deploy}", check=False, quiet=True)

    if not wait_rollout(deploy, args.timeout):
        print(f"    {RED}{deploy} was not ready within {args.timeout}s{RESET}")
        run(f"kubectl -n {NS} get pods -l app={deploy} -o wide", check=False)
        sys.exit(1)
    print(f"    {GREEN}ready{RESET}")

    if deploy == "free5gc-nrf":
        # The NRF does not register at itself: its gate is its own SBI server.
        # It accepts connections before it can write to MongoDB, hence the
        # additional delay.
        for _ in range(args.timeout // 2):
            r = run(f"kubectl -n {NS} logs deploy/{deploy} --tail=50",
                    check=False, quiet=True, timeout=60)
            if NRF_READY in strip_ansi(r.stdout):
                break
            time.sleep(2)
        print(f"    {GREEN}NRF SBI server started{RESET}; waiting {args.delay}s before the next NF")
        time.sleep(args.delay)
        return

    if deploy in REGISTER_AT_NRF:
        if wait_registration(deploy, args.timeout):
            print(f"    {GREEN}registered at the NRF{RESET}")
        else:
            # Known state: the function lost the race and holds an empty
            # instance identifier. A restart makes it register again.
            print(f"    {YELLOW}registration at the NRF not confirmed; restarting "
                  f"{deploy}{RESET}")
            run(f"kubectl -n {NS} rollout restart deploy/{deploy}", check=False)
            wait_rollout(deploy, args.timeout)
            if wait_registration(deploy, args.timeout):
                print(f"    {GREEN}registered at the NRF after the restart{RESET}")
            else:
                print(f"    {RED}{deploy} still has not registered at the NRF. "
                      f"Continuing would only make the UE registration fail later.{RESET}")
                sys.exit(1)

    time.sleep(args.delay)


def section(title):
    print(f"\n{'=' * 70}\n{title}\n{'=' * 70}")


def up(args):
    check_placeholders()
    ensure_namespace()
    other = "b" if args.scenario == "a" else "a"

    print(f"==> Removing the user plane of scenario {other.upper()} (if present)")
    for manifest, _ in SCENARIOS[other]:
        run(f"kubectl delete -f {manifest} --ignore-not-found=true", check=False, quiet=True)

    section("Control plane, in dependency order")
    for manifest, deploy in COMMON:
        bring_up(manifest, deploy, args)

    section(f"User plane - scenario {args.scenario.upper()}")
    for manifest, deploy in SCENARIOS[args.scenario]:
        bring_up(manifest, deploy, args)

    section("Edge and remote applications, Application Function")
    for manifest, deploy in APPS:
        bring_up(manifest, deploy, args)

    print(f"\n{GREEN}==> Done.{RESET} Next steps: provision the subscribers "
          f"(scripts/provision-subscribers.py), then run 'scripts/deploy.py ran'.")


def ran(args):
    check_placeholders()
    print(f"==> {RAN}")
    if run(f"kubectl apply -f {RAN}").returncode != 0:
        sys.exit(1)
    r = run(f"kubectl -n {NS} rollout status statefulset/free5gc-ueransim "
            f"--timeout={args.timeout}s", check=False, quiet=True, timeout=args.timeout + 30)
    if r.returncode != 0:
        print(f"    {RED}the UERANSIM pod was not ready within {args.timeout}s{RESET}")
        run(f"kubectl -n {NS} get pods -l epc-mode=ueransim -o wide", check=False)
        sys.exit(1)
    print(f"    {GREEN}ready{RESET}. Verify the breakout with: python3 scripts/verify-lbo.py")


def down():
    print("==> Removing every resource of the tutorial (the namespace is kept)")
    everything = [(RAN, None)] + APPS + SCENARIOS["a"] + SCENARIOS["b"] + COMMON
    for manifest, _ in everything:
        run(f"kubectl delete -f {manifest} --ignore-not-found=true", check=False, quiet=True)


def status():
    run(f"kubectl -n {NS} get pods -o wide")
    print()
    run(f"kubectl -n {NS} get svc")
    print()
    print("==> Functions that have NOT confirmed their registration at the NRF:")
    bad = [d for d in sorted(REGISTER_AT_NRF) if registered_at_nrf(d) is False]
    print("    " + (", ".join(bad) if bad else f"{GREEN}none{RESET}"))
    print()
    print("==> PFCP heartbeats in the last 40 s (expected: 1 UPF in scenario A, 2 in B)")
    for deploy in ("free5gc-upf", "free5gc-i-upf", "free5gc-psa-upf"):
        r = run(f"kubectl -n {NS} logs deploy/{deploy} --since=40s",
                check=False, quiet=True, timeout=60)
        if r.returncode == 0:
            alive = bool(re.search("heartbeat", r.stdout, re.I))
            print(f"    {deploy}: " + (f"{GREEN}heartbeat seen{RESET}" if alive
                                        else f"{YELLOW}no heartbeat{RESET}"))
    print()
    print("==> Application Function (last log lines)")
    run(f"kubectl -n {NS} logs deploy/mec-af --tail=8", check=False)


def main():
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("action", choices=["up", "ran", "down", "status"])
    parser.add_argument("scenario", nargs="?", choices=["a", "b"],
                        help="required for 'up'")
    parser.add_argument("--delay", type=int, default=10,
                        help="pause between one NF and the next, in seconds (default: 10)")
    parser.add_argument("--timeout", type=int, default=180,
                        help="timeout per NF, in seconds (default: 180)")
    args = parser.parse_args()

    os.chdir(ROOT)
    if args.action == "up":
        if not args.scenario:
            parser.error("'up' requires the scenario: a (centralized UPF) or b (edge UPF with ULCL)")
        up(args)
    elif args.action == "ran":
        ran(args)
    elif args.action == "down":
        down()
    else:
        status()


if __name__ == "__main__":
    main()
