#!/usr/bin/env python3
"""
Replaces the node placeholders of the manifests with real hostnames.

Every pod of the tutorial is pinned to a node through
"kubernetes.io/hostname". The manifests are distributed with four
placeholders, one per role (docs/02-testbed-and-prerequisites.md, section 2.4):

    CHANGE-ME-core-node     control plane, central UPF (A) or PSA-UPF (B)
    CHANGE-ME-edge-node     I-UPF, edge application, Application Function
    CHANGE-ME-ran-node      UERANSIM (gNB and UE)
    CHANGE-ME-remote-node   remote application

Usage (hostnames as listed by "kubectl get nodes"):

    python3 scripts/set-nodes.py --core srv1 --edge edge1 --ran edge2 --remote srv2

The same hostname may be given to more than one role. The replacement is done
once; later changes are made by editing the manifests.
"""
import argparse
import glob
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ROLES = ("core", "edge", "ran", "remote")


def main():
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    for role in ROLES:
        parser.add_argument(f"--{role}", required=True, metavar="HOSTNAME",
                            help=f"node that takes the '{role}' role")
    args = parser.parse_args()

    total = 0
    for path in sorted(glob.glob(os.path.join(ROOT, "manifests", "**", "*.yaml"),
                                 recursive=True)):
        with open(path) as fh:
            text = fh.read()
        new = text
        for role in ROLES:
            new = new.replace(f"hostname: CHANGE-ME-{role}-node",
                              f"hostname: {getattr(args, role)}")
        if new != text:
            count = sum(text.count(f"hostname: CHANGE-ME-{role}-node") for role in ROLES)
            total += count
            with open(path, "w") as fh:
                fh.write(new)
            print(f"{os.path.relpath(path, ROOT)}: {count} selector(s) updated")
    print(f"{total} node selector(s) updated" if total
          else "no placeholder found; the manifests were already edited")


if __name__ == "__main__":
    main()
