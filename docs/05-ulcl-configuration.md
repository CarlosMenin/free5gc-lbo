# 5. ULCL configuration

This document describes the SMF configuration of scenario B
(`manifests/scenario-b-edge-upf/smf.yaml`), which turns the edge UPF into the
Uplink Classifier of the session. It assumes the structure of
`userplaneInformation` presented in
[04-user-plane.md](04-user-plane.md), section 4.5.

## 5.1 Differences between the two scenarios

| Element | Scenario A | Scenario B |
| --- | --- | --- |
| UPF nodes | `UPF` | `I-UPF`, `PSA-UPF` |
| N3 termination | `UPF` | `I-UPF` |
| UE address pools | `UPF` | `PSA-UPF` |
| `dnaiList` | absent | `mec`, in `snssaiInfos` and in the I-UPF |
| `links` | gNB1–UPF | gNB1–I-UPF, I-UPF–PSA-UPF |
| `ulcl` | absent (false) | `true` |
| UE routing file | empty | group with members and default topology |

The remaining fields of the SMF configuration are identical.

## 5.2 DNAI

The DNAI `mec` appears in two places.

In `snssaiInfos`, it declares that the DNN of the slice has an access point
identified by `mec`:

```yaml
snssaiInfos:
  - sNssai:
      sst: 1
      sd: 010203
    dnnInfos:
      - dnn: internet
        dnaiList:
          - mec
        dns:
          ipv4: 10.96.0.10
```

In the I-UPF node, it declares that this UPF is the one that gives access to
`mec`:

```yaml
I-UPF:
  type: UPF
  nodeID: i-upf.free5gc.svc.cluster.local
  addr: i-upf.free5gc.svc.cluster.local
  sNssaiUpfInfos:
    - sNssai:
        sst: 1
        sd: 010203
      dnnUpfInfoList:
        - dnn: internet
          dnaiList:
            - mec
```

When a policy rule asks for traffic to be routed to DNAI `mec`, the SMF selects
the UPF that declares it. The DNAI must be declared for every slice in which
the Application Function announces the edge service (variable `SNSSAIS` of
`apps/mec-af.yaml`). If a rule arrives for a slice in which no UPF declares the
DNAI, the rule cannot be mapped to a UPF and no breakout takes place for the
sessions of that slice. `scripts/validate.py` reports this mismatch for
scenario B, where it indicates a configuration error. In scenario A it is the
expected situation: the Application Function runs there as well, so that the
environment is the same in both scenarios, but no UPF declares the DNAI and
every flow remains anchored at the central UPF.

The DNAI is a free-form string. The only requirement is that the value used by
the Application Function equals the one in the SMF configuration.

## 5.3 UPF nodes: interfaces and pools

```yaml
I-UPF:
  # (sNssaiUpfInfos as above; no "pools")
  interfaces:
    - interfaceType: N3
      endpoints:
        - i-upf.free5gc.svc.cluster.local
      networkInstances:
        - internet
    - interfaceType: N9
      endpoints:
        - i-upf.free5gc.svc.cluster.local
      networkInstances:
        - internet
PSA-UPF:
  type: UPF
  nodeID: psa-upf.free5gc.svc.cluster.local
  addr: psa-upf.free5gc.svc.cluster.local
  sNssaiUpfInfos:
    - sNssai:
        sst: 1
        sd: 010203
      dnnUpfInfoList:
        - dnn: internet
          pools:
            - cidr: 60.60.0.0/17
    - sNssai:
        sst: 1
        sd: 112233
      dnnUpfInfoList:
        - dnn: internet
          pools:
            - cidr: 60.60.128.0/17
  interfaces:
    - interfaceType: N9
      endpoints:
        - psa-upf.free5gc.svc.cluster.local
      networkInstances:
        - internet
```

**The I-UPF has no pool.** A branching point does not allocate UE addresses;
the address comes from the anchor of the default path. A UE address in
`60.60.0.0/16` is therefore evidence that the whole default path
(gNB → I-UPF → N9 → PSA-UPF) was established.

**The I-UPF has N3 and N9; the PSA-UPF has N9 only.** The SMF gives the gNB the
F-TEID of the N3 interface of the first UPF of the path, and reaches every
subsequent UPF through N9. Both interfaces of the I-UPF use the same endpoint,
because the UPF listens on a single socket (section 4.1).

**Pools must not overlap, even across slices.** The SMF validates the UE pools
of all UPFs globally, without separating them by S-NSSAI. Declaring
`60.60.0.0/16` for both slices makes the SMF abort at start-up with
`overlap cidr value between UPFs`. The /16 is therefore split into two disjoint
/17 pools, one per slice. Both remain inside the `60.60.0.0/16` declared in the
UPF configuration, so the route and the NAT rule of the UPF are unaffected.

## 5.4 Links and path selection

```yaml
links:
  - A: gNB1
    B: I-UPF
  - A: I-UPF
    B: PSA-UPF
```

The links declare the default path: the gNB is connected to the I-UPF, and the
I-UPF to the PSA-UPF. The following properties of the path selection of the SMF
in v4.2.3 are relevant when the topology is extended
([11-extensions.md](11-extensions.md)):

- The search starts at the access node and visits its neighbours **in the order
  in which the links are listed**. With more than one link from the gNB, the
  first one that leads to a valid path is used.
- A UPF that does not serve the slice of the session is skipped.
- The UPFs at the end of the path become anchors and must have a pool for the
  slice and DNN of the session.
- The N3 endpoint delivered to the gNB is that of the first UPF of the path.

## 5.5 The ULCL switch and the UE routing file

```yaml
ulcl: true
```

This option enables the Uplink Classifier. Without it the UE routing file is
ignored in its entirety, and the breakout rules have no effect.

The UE routing file is a second configuration file, passed to the SMF with the
`-u` option:

```yaml
command: ["./smf", "-c", "/etc/free5gc/smfConfig.yaml", "-u", "/free5gc/ue/ueRouting.yaml"]
```

Its content in scenario B:

```yaml
info:
  version: 1.0.7
  description: UE routing - local breakout through the ULCL

ueRoutingInfo:
  UE1:
    members:
      - imsi-208930000000000
      - imsi-208930000000001
      - imsi-208930000000002
      - imsi-208930000000003
      - imsi-208930000000004
    topology:
      - A: gNB1
        B: I-UPF
      - A: I-UPF
        B: PSA-UPF
```

- **`UE1`** is the name of a group; any name can be used and several groups can
  be declared.
- **`members`** lists the subscribers to which the group applies, that is, its
  `topology` and its static rules (section 5.7). A UE that is not listed in any
  group follows the default path derived from `links` and receives no static
  rule. To change the number of subscribers, the list is regenerated with
  `scripts/gen-uerouting.py`.
- **`topology`** is the default path of the group. The node names must exist in
  `upNodes`.

The file is read when the SMF starts. After changing it, the SMF has to be
restarted.

The breakout rule itself is not declared in this file in the default
configuration of the repository: it arrives at runtime
([06-traffic-influence.md](06-traffic-influence.md)). The `ulcl` option and
the `members` and `topology` of this file remain in effect.

## 5.6 Scenario A

The baseline (`manifests/scenario-a-central-upf/smf.yaml`) declares one UPF and
one link, has no `ulcl` option and no DNAI.

It still provides a UE routing file. The SMF of v4.2.3 reads that file at
start-up unconditionally, before checking whether the Uplink Classifier is
enabled. Without `-u`, it falls back to `./config/uerouting.yaml`, which the
image does not contain, and exits with:

```
SMF Run error: ReadConfig [] Error: open ./config/uerouting.yaml: no such file or directory
```

The file of scenario A is therefore intentionally empty:

```yaml
info:
  version: 1.0.7
  description: UE routing - scenario A (no ULCL, intentionally empty)

ueRoutingInfo: {}
```

## 5.7 Static breakout rule

The breakout can also be configured without the NEF and the Application
Function, by adding a `specificPath` to the group in the UE routing file:

```yaml
ueRoutingInfo:
  UE1:
    members:
      - imsi-208930000000000
    topology:
      - A: gNB1
        B: I-UPF
      - A: I-UPF
        B: PSA-UPF
    specificPath:
      - dest: 10.96.100.100/32
        path: [I-UPF]
```

- **`dest`** is the destination to divert. It must be in CIDR notation; a host
  name is not accepted.
- **`path`** is the list of UPFs that the matching traffic traverses. With
  `[I-UPF]` the traffic leaves the user plane at the I-UPF.

The value of `dest` is the current ClusterIP of the edge Service:

```bash
kubectl -n free5gc get svc mec-app -o jsonpath='{.spec.clusterIP}'
python3 scripts/gen-uerouting.py -n 5 --dest <ClusterIP>/32 > /tmp/ue-routing.yaml
kubectl apply -f /tmp/ue-routing.yaml
kubectl -n free5gc rollout restart deploy/free5gc-smf
```

This alternative is useful to isolate a problem: if the breakout works with a
static rule and not with Traffic Influence, the fault is in the chain
AF → NEF → UDR → PCF → SMF and not in the user plane. Its limitations are the
reason it is not the default:

- the address is a literal that changes whenever the Service is recreated;
- the file is read only at start-up, so every change requires restarting the
  SMF, a control-plane function on which the established sessions depend;
- it does not exercise the standardized procedure by which an application
  declares its location to the network.

When the static rule is used together with the Application Function, the rule
is redundant. To test the static rule alone, the Application Function is
removed (`kubectl -n free5gc delete deploy mec-af`) and its subscriptions are
deleted as described in section 6.8.

## 5.8 Resulting behaviour

With the configuration above and a breakout rule in place, a single PDU session
has two forwarding behaviours:

| Uplink packet | Handling at the I-UPF | Exit point |
| --- | --- | --- |
| Destination matches the edge service | Decapsulated, source-translated, sent through the pod interface | I-UPF (edge node) |
| Any other destination | Forwarded over N9 without terminating the tunnel | PSA-UPF (core node) |

The UE keeps one address and one session. Installing or removing the rule does
not require a new registration or a new session.

The counters of the gtp5g interface reflect this split and are the basis of the
verification in [09-verification.md](09-verification.md): the I-UPF counts only
the packets it decapsulates locally, and the PSA-UPF counts only the packets of
the default path.

Next: [06-traffic-influence.md](06-traffic-influence.md).
