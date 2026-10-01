# Local Breakout on free5GC

A reproducible configuration of **Local Breakout (LBO)** on free5GC v4.2.3,
deployed on Kubernetes, in which an edge UPF diverts the traffic destined to an
edge application while the rest of the same PDU session continues to the
anchor UPF in the core.

The repository contains the complete set of manifests, the supporting scripts,
and a description of every configuration decision. It covers two mechanisms:

- the **Uplink Classifier (ULCL)** of the free5GC SMF, which places an
  intermediate UPF (I-UPF) at the edge as the branching point of the session;
- **Traffic Influence** through the NEF, by which an Application Function
  announces the address of the edge application at runtime, so that no literal
  address is written in the core configuration.

A centralized baseline (a single UPF in the core) is included for comparison.

## Scenarios

```
Scenario A: centralized UPF (baseline)

  UE ── gNB ──N3────────────────────► UPF (core) ──N6──► remote application
                                          │
                                          └──N6──► edge application
                                                   (back across the transport network)

Scenario B: edge UPF with ULCL (local breakout)

  UE ── gNB ──N3──► I-UPF (edge) ──N9──► PSA-UPF (core) ──N6──► remote application
                       │
                       └──N6──► edge application
```

In both scenarios the UE holds a single PDU session with a single address. In
scenario B, uplink packets addressed to the edge application are matched at the
I-UPF and leave the 5G user plane there; every other packet follows the default
path to the PSA-UPF.

## Components

| Component | Implementation | Node role |
| --- | --- | --- |
| Control plane | free5GC v4.2.3: NRF, AMF, SMF, AUSF, UDM, UDR, PCF, NSSF, NEF, web console, MongoDB | core |
| User plane, scenario A | one UPF (`free5gc-upf`) | core |
| User plane, scenario B | I-UPF (`free5gc-i-upf`) and PSA-UPF (`free5gc-psa-upf`) | edge and core |
| Application Function | `mec-af` (Python, standard library only) | edge |
| Edge application | `mec-app` (UDP echo server) | edge |
| Remote application | `remote-app` (UDP echo server) | remote |
| UE and RAN | UERANSIM (gNB and UE in one pod) | ran |

## Repository layout

```
manifests/
  namespace.yaml
  common/                    control plane (one file per network function)
  scenario-a-central-upf/    SMF and UPF of the baseline
  scenario-b-edge-upf/       SMF, I-UPF and PSA-UPF of the local breakout
  apps/                      edge and remote applications, Application Function
  ran/                       UERANSIM
  monitoring/                optional ServiceMonitors (Prometheus Operator)
af/mec_af.py                 Application Function (Traffic Influence client)
probe/probe.py               UDP echo probe used by the verification
scripts/
  set-nodes.py               writes the node hostnames into the manifests
  validate.py                static consistency checks on the manifests
  deploy.py                  ordered deployment and removal
  provision-subscribers.py   subscriber provisioning in MongoDB
  gen-uerouting.py           UE routing file for N subscribers
  gen-configmaps.py          embeds af/ and probe/ sources into ConfigMaps
  verify-lbo.py              verification of the breakout through UPF counters
docs/                        the tutorial, in reading order
```

## Quick start

Prerequisites: a Kubernetes cluster, `kubectl` configured for it, the `gtp5g`
kernel module loaded on every node that hosts a UPF, and Python 3.9 or newer on
the workstation. The complete list is in
[docs/02-testbed-and-prerequisites.md](docs/02-testbed-and-prerequisites.md).

```bash
pip install -r scripts/requirements.txt

# 1. Assign the cluster nodes to the four roles.
python3 scripts/set-nodes.py --core <node> --edge <node> --ran <node> --remote <node>

# 2. Check the manifests.
python3 scripts/validate.py

# 3. Deploy scenario B: control plane, both UPFs, applications and AF.
python3 scripts/deploy.py up b

# 4. Provision the subscribers (before starting the RAN).
kubectl -n free5gc port-forward svc/mongo 27017:27017 &
python3 scripts/provision-subscribers.py --count 5 --clean

# 5. Start UERANSIM.
python3 scripts/deploy.py ran

# 6. Verify that the edge traffic leaves the user plane at the I-UPF.
python3 scripts/verify-lbo.py
```

The baseline is deployed with `python3 scripts/deploy.py up a`; the same
verification script then confirms that both flows are anchored at the central
UPF. Each step is explained in
[docs/08-deployment.md](docs/08-deployment.md) and
[docs/09-verification.md](docs/09-verification.md).

## Documentation

| Document | Content |
| --- | --- |
| [01-concepts.md](docs/01-concepts.md) | Local breakout, UPF roles, ULCL, DNAI, Traffic Influence; the two scenarios |
| [02-testbed-and-prerequisites.md](docs/02-testbed-and-prerequisites.md) | Software versions, cluster requirements, gtp5g, node roles, network parameters |
| [03-control-plane.md](docs/03-control-plane.md) | Configuration of each network function; start-up order |
| [04-user-plane.md](docs/04-user-plane.md) | UPF configuration, forwarding and NAT, user-plane topology in the SMF, usage reporting |
| [05-ulcl-configuration.md](docs/05-ulcl-configuration.md) | The ULCL configuration, field by field; path selection; static breakout rule |
| [06-traffic-influence.md](docs/06-traffic-influence.md) | Dynamic breakout rule: NEF, subscription content, Application Function |
| [07-ran-and-subscribers.md](docs/07-ran-and-subscribers.md) | Subscriber provisioning and UERANSIM |
| [08-deployment.md](docs/08-deployment.md) | Deployment procedure, automated and manual |
| [09-verification.md](docs/09-verification.md) | Layered checks and the verification of the breakout |
| [10-troubleshooting.md](docs/10-troubleshooting.md) | Symptoms, causes and corrections |
| [11-extensions.md](docs/11-extensions.md) | More subscribers, more edge services, a second edge node, a slice that bypasses the edge UPF, monitoring |

## Scope and provenance

The configuration of the network functions (control plane, UPFs, SMF with ULCL,
NEF, Application Function logic, UERANSIM) is the one used in the reference
testbed described in
[docs/02-testbed-and-prerequisites.md](docs/02-testbed-and-prerequisites.md),
where the breakout was verified with the counter-based procedure of
[docs/09-verification.md](docs/09-verification.md).

Three elements were adapted for this repository: node names were replaced by
placeholders, the workload generator of the reference testbed was replaced by
a minimal UDP echo probe, and the verification was reduced to a standalone
script (`scripts/verify-lbo.py`). The round-trip times printed by the probe are
indicative only; a latency study requires a dedicated measurement tool and a
calibration of the environment.

Addresses, identifiers and keys in the manifests (PLMN 208/93, the subscriber
key and OPc, the SUCI profile keys) are the public test values distributed
with free5GC and UERANSIM. They must not be used outside a laboratory.

## License

MIT. See [LICENSE](LICENSE).
