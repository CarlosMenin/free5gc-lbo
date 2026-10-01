#!/usr/bin/env python3
"""
Generates the ue-routing-info ConfigMap of scenario B for N subscribers.

A group of the UE routing file applies only to the subscribers listed in
ueRoutingInfo.<group>.members, so changing the number of UEs requires
regenerating that list. See docs/05-ulcl-configuration.md, section 5.5.

The destination of the local breakout is not part of this file: it is
delivered at runtime through Traffic Influence. --dest generates the static
alternative (specificPath), for use without the NEF (section 5.7).

Usage:
    python3 scripts/gen-uerouting.py -n 50 > /tmp/ue-routing.yaml
    kubectl apply -f /tmp/ue-routing.yaml
    kubectl -n free5gc rollout restart deployment/free5gc-smf

The generated ConfigMap replaces the one embedded in
manifests/scenario-b-edge-upf/smf.yaml; applying that manifest again restores
the five-subscriber list.
"""
import argparse

TEMPLATE = """apiVersion: v1
kind: ConfigMap
metadata:
  name: ue-routing-info
  namespace: {ns}
data:
  ueRouting.yaml: |
    info:
      version: 1.0.7
      description: UE routing - local breakout through the ULCL ({n} UEs)

    ueRoutingInfo:
      UE1:
        members:
{members}
        topology:
          - A: gNB1
            B: I-UPF
          - A: I-UPF
            B: PSA-UPF
{specific}"""

SPECIFIC = """        specificPath:
          - dest: {dest}
            path: [I-UPF]
"""


def main():
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("-n", "--num-ue", type=int, default=5,
                        help="number of subscribers in the ULCL group (default: 5)")
    parser.add_argument("--imsi-base", default="208930000000000",
                        help="first IMSI (default: 208930000000000)")
    parser.add_argument("--namespace", default="free5gc")
    parser.add_argument("--dest", default=None,
                        help="static breakout destination in CIDR notation (current "
                             "ClusterIP of the edge Service followed by /32). Omitted by "
                             "default: the rule is delivered through Traffic Influence")
    args = parser.parse_args()

    base = int(args.imsi_base)
    width = len(args.imsi_base)
    members = "\n".join(
        f"          - imsi-{str(base + i).zfill(width)}" for i in range(args.num_ue)
    )
    specific = SPECIFIC.format(dest=args.dest) if args.dest else ""
    print(TEMPLATE.format(ns=args.namespace, n=args.num_ue, members=members,
                          specific=specific), end="")


if __name__ == "__main__":
    main()
