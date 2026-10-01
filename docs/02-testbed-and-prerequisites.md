# 2. Testbed and prerequisites

## 2.1 Software versions

| Software | Version | Source |
| --- | --- | --- |
| free5GC network functions | v4.2.3 | images `free5gc/<nf>:v4.2.3` (amf, smf, upf, nrf, ausf, udm, udr, pcf, nssf, nef, webui) |
| UERANSIM | build distributed with free5GC v4.2.3 | image `free5gc/ueransim:v4.2.3` |
| MongoDB | 4.0.5 | image `free5gmano/nextepc-mongodb` |
| Application Function, probe | sources in this repository | image `python:3.12-slim` (no additional package) |
| gtp5g | a release that builds against the kernel of the UPF nodes | <https://github.com/free5gc/gtp5g> |

The configuration files follow the schema versions required by v4.2.3
(`info.version` 1.0.7 for the SMF, 1.0.3 for the UPF, 1.1.0 for the UDR, 1.0.3
for the web console). Other free5GC releases may require different versions or
fields; `scripts/validate.py` checks the ones listed here.

## 2.2 Cluster requirements

- **Privileged containers.** The UPF and UERANSIM containers run with
  `privileged: true`: the UPF creates the gtp5g network device and writes to
  `/proc/sys`, and UERANSIM creates TUN interfaces.
- **SCTP.** N2 between the gNB and the AMF uses SCTP (port 38412) through a
  Kubernetes Service. The cluster and its network plugin must support Services
  with `protocol: SCTP`.
- **UDP Services.** GTP-U (2152) and PFCP (8805) are carried through ClusterIP
  Services.
- **Cluster DNS.** Every network function is addressed by its Service name
  (`<nf>.free5gc.svc.cluster.local`), and the Application Function discovers
  the edge application through DNS.
- **Persistent storage.** `manifests/common/mongo.yaml` requests a 1 Gi
  `ReadWriteMany` volume from the storage class `longhorn`. The storage class
  name is specific to the reference cluster and must be replaced by one that
  exists in the target cluster.
- **Namespace.** All resources are created in the namespace `free5gc`. The
  name is part of every FQDN in the configuration files; using another
  namespace requires replacing it in all manifests. `scripts/deploy.py`
  creates the namespace when it does not exist.
- **Prometheus Operator (optional).** The network functions expose metrics on
  port 9091. The `ServiceMonitor` objects are kept in a separate file
  (`manifests/monitoring/servicemonitors.yaml`) because they can only be
  applied where the Prometheus Operator CRDs are installed
  ([11-extensions.md](11-extensions.md), section 11.6).

## 2.3 gtp5g

The free5GC UPF forwards user traffic in the Linux kernel through the
[gtp5g](https://github.com/free5gc/gtp5g) module. A kernel module belongs to
the node, not to the pod: **it must be built and loaded on every node that
hosts a UPF** (the core node in scenario A; the core node and the edge node in
scenario B).

```bash
git clone https://github.com/free5gc/gtp5g.git
cd gtp5g
make
sudo make install
sudo modprobe gtp5g
```

The module has to be loaded again after a reboot unless it is added to the
modules loaded at boot (for example in `/etc/modules-load.d/`).

When the module is missing, the UPF pod still starts and no network function
reports an error; the data path does not exist. The list of kernel
modules is not namespaced, so it can be checked from inside the UPF pod once it
is running, without logging in to the node:

```bash
kubectl -n free5gc exec deploy/free5gc-i-upf -- grep gtp5g /proc/modules
```

## 2.4 Node roles and placement

Every pod is pinned to a node with a `nodeSelector` on
`kubernetes.io/hostname`. The manifests are distributed with one placeholder
per role:

| Placeholder | Role | Workloads |
| --- | --- | --- |
| `CHANGE-ME-core-node` | Core site | AMF, SMF, NRF, AUSF, UDM, UDR, PCF, NSSF, NEF, MongoDB, web console; central UPF (scenario A) or PSA-UPF (scenario B) |
| `CHANGE-ME-edge-node` | Edge node | I-UPF (scenario B), edge application, Application Function |
| `CHANGE-ME-ran-node` | UE and RAN emulation | UERANSIM (gNB and UE), probe client |
| `CHANGE-ME-remote-node` | Remote service | Remote application |

The placeholders are replaced with the hostnames shown by
`kubectl get nodes`:

```bash
python3 scripts/set-nodes.py --core <node> --edge <node> --ran <node> --remote <node>
```

`scripts/deploy.py` refuses to run while a placeholder remains.

The breakout mechanism works with any placement, provided gtp5g is loaded on
the nodes that host a UPF; the same hostname may even be given to several
roles for a functional test. Placement determines whether a *measurement* taken
on the testbed is meaningful. The layout above follows these rules:

1. **The I-UPF and the edge application share a node.** The edge node is the
   place where traffic stays local: after the breakout, the packet reaches the
   application without leaving the machine. If the application were on another
   node, the packet would leave the UPF and still cross a network link.
2. **The anchor in the core and the control plane share a node, on the other
   side of the transport network.** The edge node and the core node must be
   separated by the link that plays the role of the backhaul; otherwise the
   default path does not cross it and the two scenarios cannot differ.
3. **UERANSIM runs on its own node, on the edge side of the transport
   network.** A subscriber does not reside inside the edge host, and on small
   machines the emulated gNB and UE compete with the UPF for CPU. Placing
   UERANSIM in the core site would make the emulated access link itself cross
   the backhaul.
4. **The remote application is neither on the edge side nor on the core node.**
   It represents a service reached *through* the core. On the core node it
   would save the N6 hop that the edge path pays.
5. **The SMF is on the same node in both scenarios**, so that the only change
   between them is the user plane.

### Reference testbed

The configuration in this repository was developed on the following
environment, given here as an indication of the resources involved:

| Role | Hardware |
| --- | --- |
| UE and RAN emulation | Intel Core i7-5500U (2 cores, 4 threads), 4 GB RAM, 1 GbE |
| Edge node | Intel Core i7-5500U (2 cores, 4 threads), 4 GB RAM, 1 GbE |
| Core site | data-center server, 80 hardware threads, 197 GB RAM |
| Remote service | data-center server, 40 hardware threads, 64 GB RAM |

The two edge-side machines share one layer-2 segment; the data-center machines
are in a different subnet, reached through a router. The cluster network is a
VXLAN overlay with a 1450-byte MTU.

## 2.5 Network parameters

The values below are used consistently across the manifests. Changing one of
them requires changing every file listed in
[03-control-plane.md](03-control-plane.md), section 3.5.

| Parameter | Value |
| --- | --- |
| PLMN | MCC 208, MNC 93 |
| Tracking area code | 1 (`000001`) |
| Slices (S-NSSAI) | SST 1 / SD `010203` and SST 1 / SD `112233` |
| DNN | `internet` |
| UE address pools | `60.60.0.0/17` (slice `010203`) and `60.60.128.0/17` (slice `112233`); `60.60.0.0/16` in the UPF |
| DNAI of the edge domain | `mec` |
| DNS server announced to the UE | `10.96.0.10` (cluster DNS of the reference testbed) |
| Subscribers | `imsi-208930000000000` to `imsi-208930000000004` |
| SBI | HTTP, port 8000 on every network function |
| N2 | SCTP 38412 |
| N3 / N9 | UDP 2152 (GTP-U) |
| N4 | UDP 8805 (PFCP) |
| Probe | UDP 9000 |

The DNS address is the ClusterIP of the cluster DNS Service and differs
between clusters. It is obtained with:

```bash
kubectl -n kube-system get svc kube-dns -o jsonpath='{.spec.clusterIP}'
```

and replaces `10.96.0.10` in the `dns` blocks of both SMF manifests.

## 2.6 Workstation tools

- `kubectl`, configured for the cluster, with permission to create the
  resources above and to use `exec` and `port-forward` in the namespace.
- Python 3.9 or newer.
- The packages in `scripts/requirements.txt`:

```bash
pip install -r scripts/requirements.txt
```

`pymongo` is pinned below 4.14 because later releases no longer connect to
MongoDB 4.0, the version of the image used by the manifests.

Next: [03-control-plane.md](03-control-plane.md).
