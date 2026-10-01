# 4. User plane

This document describes what is common to every UPF of the repository and how
the SMF is told about the user-plane topology. The settings that are specific
to the Uplink Classifier are in
[05-ulcl-configuration.md](05-ulcl-configuration.md).

| Scenario | File | Deployment | Service | Role |
| --- | --- | --- | --- | --- |
| A | `scenario-a-central-upf/upf.yaml` | `free5gc-upf` | `upf` | Single anchor |
| B | `scenario-b-edge-upf/i-upf.yaml` | `free5gc-i-upf` | `i-upf` | Uplink Classifier and local anchor |
| B | `scenario-b-edge-upf/psa-upf.yaml` | `free5gc-psa-upf` | `psa-upf` | Anchor of the default path |

Each UPF manifest contains two ConfigMaps (the UPF configuration and a start-up
script), a Deployment and a Service.

## 4.1 UPF configuration

The three UPFs use the same configuration structure:

```yaml
version: 1.0.3

pfcp:
  addr: i-upf-pod                               # (1)
  nodeID: i-upf.free5gc.svc.cluster.local       # (2)
  retransTimeout: 1s
  maxRetrans: 3

gtpu:
  forwarder: gtp5g
  ifList:                                       # (3)
    - addr: 0.0.0.0
      type: N3

dnnList:                                        # (4)
  - dnn: internet
    cidr: 60.60.0.0/16
```

1. **`pfcp.addr`** is the address on which the UPF listens for PFCP. It must
   not be `0.0.0.0`. The pod hostname (`spec.hostname` of the Deployment) is
   used; it resolves to the pod address through `/etc/hosts`.
2. **`pfcp.nodeID`** is the identity the UPF announces in the PFCP association.
   It must be equal to the `nodeID` declared for that UPF in the SMF
   configuration. When the two differ, both processes start, neither reports an
   error, and the association is never established. The FQDN of the UPF Service
   is used on both sides.
3. **`gtpu.ifList`**: the go-upf of v4.2.3 uses only the first entry of the list;
   additional entries have no effect. A single entry with `0.0.0.0` makes the
   UPF accept GTP-U on every address of the pod, which covers N3 and N9 with
   one socket. The `type` field does not restrict what the socket accepts.
4. **`dnnList`**: each CIDR becomes a route on the gtp5g interface, which is how
   downlink packets addressed to a UE are steered into the tunnel. A UPF must
   therefore list every UE pool whose traffic traverses it. This applies to the
   I-UPF as well: it does not allocate addresses (the PSA-UPF does), but
   downlink packets of the edge application enter the tunnel there. A pool
   missing from the I-UPF means no route and no downlink for the diverted
   traffic.

The UPF pool (`60.60.0.0/16`) is the union of the two per-slice pools declared
in the SMF (`60.60.0.0/17` and `60.60.128.0/17`). The reason for the split is
given in section 5.3.

## 4.2 Start-up script

The UPF container does not start the `upf` binary directly; it runs a script
mounted from the second ConfigMap:

```bash
#!/bin/bash
echo 1 > /proc/sys/net/ipv4/ip_forward

iptables -t nat -A POSTROUTING -o eth0 -j MASQUERADE
iptables -I FORWARD 1 -j ACCEPT
iptables -A FORWARD -p tcp -m tcp --tcp-flags SYN,RST SYN -j TCPMSS --set-mss 1400

./upf -c /etc/free5gc/upfConfig.yaml
```

**IP forwarding.** After decapsulation, an uplink packet has to leave the UPF
through the pod interface (`eth0`) towards the data network. From the point of
view of the kernel it is a *forwarded* packet, not a locally delivered one.
With `ip_forward=0` the kernel drops it before the `FORWARD` chain is
evaluated, so the `ACCEPT` rule alone does not help. The failure is silent: the
session is established, the PFCP association is up, the tunnel interface of the
UE exists, and no network function logs anything. The only sign is in the
counters of the gtp5g interface, where RX increases and TX stays at zero:

```bash
kubectl -n free5gc exec deploy/free5gc-upf -- cat /proc/net/dev | grep upfgtp
kubectl -n free5gc exec deploy/free5gc-upf -- cat /proc/sys/net/ipv4/ip_forward
```

The `free5gc/upf` image does not include the `sysctl` binary, hence the write
to `/proc`, which is permitted because the container is privileged. The
`securityContext.sysctls` field of Kubernetes is not an alternative unless the
kubelet is configured to allow that sysctl.

**NAT.** UE addresses (`60.60.0.0/16`) are not routable in the cluster network.
`MASQUERADE` rewrites the source of the decapsulated packets to the address of
the UPF pod, so that the replies of the application return to the UPF. The rule
is needed on every UPF that hands traffic to a data network, which in scenario
B includes the I-UPF: it is the exit point of the edge traffic.

A consequence is that applications see the address of the UPF pod as the
source of the traffic, not the UE address.

**MSS clamping.** The last rule limits the TCP MSS to 1400 bytes so that TCP
segments fit in the GTP-U tunnel without fragmentation.

## 4.3 Container privileges

```yaml
securityContext:
  privileged: true
  capabilities:
    add: ["NET_ADMIN", "SYS_MODULE"]
volumeMounts:
  - name: dev-net-tun
    mountPath: /dev/net/tun      # hostPath
```

The UPF creates the gtp5g device (`upfgtp`) in the network namespace of the
pod, edits iptables and writes to `/proc/sys`. The kernel module itself is
provided by the node (section 2.3).

## 4.4 Services for N3, N9 and N4

Each UPF has a ClusterIP Service exposing UDP 2152 (GTP-U) and UDP 8805 (PFCP),
and the SMF Service exposes UDP 8805. The SMF and the gNB reach the UPFs
through these Services:

- **PFCP.** The SMF sends the association request to the Service FQDN given in
  `addr`.
- **GTP-U.** The SMF resolves the FQDN in `interfaces.endpoints` and delivers
  the resulting address, which is the ClusterIP of the Service, to the gNB in
  the F-TEID. The gNB sends uplink GTP-U to the ClusterIP and the cluster
  forwards it to the UPF pod.

One effect of this arrangement is visible in packet captures at the gNB:
downlink GTP-U packets arrive with the **ClusterIP** of the UPF as source, not
the address of the UPF pod. The downlink flow uses the same pair of ports
(2152) as the uplink flow, so the connection tracking of the node treats it as
the reply direction of the uplink flow and reverses the address translation.
Any filter that checks the source of GTP-U at the gNB has to accept both the
pod address and the ClusterIP.

## 4.5 User-plane topology in the SMF

The SMF learns the user plane from the `userplaneInformation` block. The
baseline (scenario A) shows its structure with the minimum number of elements
(the excerpt is abridged; the complete block is in
`scenario-a-central-upf/smf.yaml`):

```yaml
userplaneInformation:
  upNodes:
    gNB1:
      type: AN
    UPF:
      type: UPF
      nodeID: upf.free5gc.svc.cluster.local      # equal to pfcp.nodeID of the UPF
      addr: upf.free5gc.svc.cluster.local        # where the SMF sends PFCP
      sNssaiUpfInfos:
        - sNssai: { sst: 1, sd: 010203 }
          dnnUpfInfoList:
            - dnn: internet
              pools:
                - cidr: 60.60.0.0/17
        - sNssai: { sst: 1, sd: 112233 }
          dnnUpfInfoList:
            - dnn: internet
              pools:
                - cidr: 60.60.128.0/17
      interfaces:
        - interfaceType: N3
          endpoints:
            - upf.free5gc.svc.cluster.local
          networkInstances:
            - internet
  links:
    - A: gNB1
      B: UPF
```

- **`upNodes`** lists the access node (`type: AN`) and the UPFs. The node names
  (`gNB1`, `UPF`) are local labels; they are referenced by `links` and by the UE
  routing file.
- **`sNssaiUpfInfos`** declares which slices and DNNs a UPF serves and, for an
  anchor, the pools from which it allocates UE addresses.
- **`interfaces.endpoints`** is the address placed in the F-TEID sent to the
  peer (the gNB for N3, another UPF for N9). It must be reachable by that peer
  and therefore cannot be `0.0.0.0`, even though the UPF itself listens on
  `0.0.0.0`.
- **`links`** declares the edges of the topology graph. The SMF derives the
  path of a session from them.

The SMF also carries its own PFCP identity:

```yaml
pfcp:
  nodeID: smf.free5gc.svc.cluster.local
  listenAddr: 0.0.0.0
  externalAddr: smf.free5gc.svc.cluster.local
  heartbeatInterval: 5s
```

## 4.6 Usage reporting (URR)

The SMF asks each UPF to report traffic usage through Usage Reporting Rules.
The example configuration of free5GC sets

```yaml
urrPeriod: 10
urrThreshold: 1000      # bytes
```

With a volume threshold of 1000 bytes, the UPF sends one PFCP usage report for
every 1000 bytes forwarded. The report rate grows with the traffic rate:

| Traffic | Usage reports per second |
| --- | --- |
| 6 Mbit/s | about 750 |
| 50 Mbit/s | about 6250 |

The SMF does not sustain that rate of PFCP transactions. In the reference
testbed a single I-UPF emitted 27,814 reports in one run, after which the SMF
logged, in sequence:

```
[LIB][PFCP]  Sequence number 99911 already exist!
[LIB][PFCP]  PutTransaction error: insert tx error: duplicate sequence number
[SMF][CTX]   Allocate PDR Error: UPF[...] not associated with SMF
[SMF][Main]  Failed to setup an association with UPF[...]: retry-out
```

The visible effects were a data path that stopped in the middle of a run, a
downlink that disappeared while the uplink kept working, new PDU sessions
failing with `no response from the network`, and UEs that stayed registered
without ever obtaining a tunnel interface.

The association does not recover by itself: the UPF keeps refusing the
association requests of the SMF until the UPF is restarted
(`kubectl -n free5gc rollout restart deploy/free5gc-i-upf`). Restarting only
the SMF leaves the scenario partially broken.

The testbed has no charging function, so the reports are produced, transported
and discarded. Both SMF manifests therefore raise the thresholds to values that
are not reached during an experiment:

```yaml
urrPeriod: 3600
urrThreshold: 1000000000
requestedUnit: 1000000000
```

The rules still exist, as the SMF needs them to build a session; they are only
prevented from triggering. After the change no usage report was observed under
load.

Next: [05-ulcl-configuration.md](05-ulcl-configuration.md).
