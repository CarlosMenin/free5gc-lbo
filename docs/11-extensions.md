# 11. Extensions

This document describes changes that go beyond the base configuration.
Section 11.4 describes a variant that was run on the reference testbed.
Section 11.3 is an outline that was not implemented. The remaining sections
describe options already supported by the manifests and scripts.

## 11.1 More subscribers

Three places must agree on the set of IMSIs:

1. **Database.** Provision the subscribers:

   ```bash
   python3 scripts/provision-subscribers.py --count 50 --clean
   ```

2. **UE routing file.** Regenerate the `members` list and restart the SMF:

   ```bash
   python3 scripts/gen-uerouting.py -n 50 > /tmp/ue-routing.yaml
   kubectl apply -f /tmp/ue-routing.yaml
   kubectl -n free5gc rollout restart deploy/free5gc-smf
   ```

   Applying `manifests/scenario-b-edge-upf/smf.yaml` again restores the list of
   five subscribers embedded in that manifest. For a permanent change, the
   `members` list of the manifest is edited instead.

3. **UERANSIM.** Set `NUM_UE` in the `ue` container of
   `manifests/ran/ueransim.yaml` and apply the manifest. One `nr-ue` process
   registers `NUM_UE` sequential IMSIs and creates one tunnel interface per UE
   (`uesimtun0`, `uesimtun1`, ...). The verification script uses the first UE
   by default; another one is selected with `--imsi` and `--tun`.

## 11.2 More edge services

Any Service of the namespace can be announced as an edge service by adding its
name to `SERVICES` in `manifests/apps/mec-af.yaml`:

```yaml
- name: SERVICES
  value: "mec-app,inference"
```

After the manifest is applied, the Application Function creates one
subscription per service and slice. A Service in another namespace is given by
its FQDN (`inference.other-namespace.svc.cluster.local`), and a destination
outside the cluster by its IPv4 address.

Two conditions apply to an edge service:

- its pods should run on the edge node, for the reason given in section 2.4;
- its Service must not pin a `clusterIP`; the address is discovered at runtime.

The UDP echo servers of this repository can be replaced by any workload. A
closed-loop traffic generator that measures motion-to-photon latency, PhotonPath,
is distributed separately and can take the place of `mec-app` and `remote-app`;
its edge Service is then the name listed in `SERVICES`.

## 11.3 A second edge node

Adding an edge node is not only a matter of placement. It requires:

- a second I-UPF Deployment and Service (a copy of `i-upf.yaml` with other
  names, `nodeID` and node), with gtp5g loaded on the new node;
- a second emulated gNB, with a different gNB identity (`nci`), since two gNBs
  with the same identity conflict at the AMF;
- in the SMF configuration, a second access node and a second UPF node in
  `upNodes`, with its own DNAI, and the corresponding entries in `links`;
- a second group in the UE routing file, with the subscribers served by the new
  edge node and their topology;
- an Application Function (or a second set of subscriptions) that announces
  the services of the new edge node with the new DNAI.

This configuration was not implemented on the reference testbed.

## 11.4 A slice that bypasses the edge UPF

In some experiments part of the UEs must not traverse the edge UPF at all: for
example, UEs of another cell that generate background traffic through the
anchor in the core. Since a Traffic Influence subscription with `anyUeInd`
covers every UE of a DNN and slice, and since the default data path of a
session may be derived from the global `links` regardless of the UE routing
group (section 7.1), excluding those UEs through a separate group of the UE
routing file is not sufficient. The reliable separation is by **slice**.

The base configuration declares two slices and uses one (`010203`). The second
slice (`112233`) is turned into a slice that is served only by the PSA-UPF,
with a direct N3 path from the gNB to the PSA-UPF. The changes to
`manifests/scenario-b-edge-upf/smf.yaml` are:

1. **Remove the slice from the I-UPF** and the DNAI from the slice:

   ```yaml
   snssaiInfos:
     - sNssai: { sst: 1, sd: 112233 }
       dnnInfos:
         - dnn: internet
           # no dnaiList: this slice has no breakout
   ```

   ```yaml
   I-UPF:
     sNssaiUpfInfos:
       - sNssai: { sst: 1, sd: 010203 }      # only the slice with breakout
         dnnUpfInfoList:
           - dnn: internet
             dnaiList:
               - mec
   ```

2. **Give the PSA-UPF an N3 interface**, in addition to N9:

   ```yaml
   PSA-UPF:
     interfaces:
       - interfaceType: N9
         endpoints:
           - psa-upf.free5gc.svc.cluster.local
         networkInstances:
           - internet
       - interfaceType: N3
         endpoints:
           - psa-upf.free5gc.svc.cluster.local
         networkInstances:
           - internet
   ```

   The gNB receives the N3 endpoint of the first UPF of the path. For the
   second slice the first UPF is the PSA-UPF, which therefore needs an N3
   interface in the SMF configuration. Nothing changes in the UPF itself: it
   already listens on `0.0.0.0` (section 4.1).

3. **Add a link from the gNB to the PSA-UPF, after the existing links:**

   ```yaml
   links:
     - A: gNB1
       B: I-UPF
     - A: I-UPF
       B: PSA-UPF
     - A: gNB1
       B: PSA-UPF
   ```

   **The order is significant.** The SMF visits the neighbours of the access
   node in the order of this list (section 5.4). For slice `010203` it finds the
   I-UPF first and builds gNB → I-UPF → PSA-UPF, as before. For slice `112233`
   the I-UPF does not serve the slice and is skipped, so the path becomes
   gNB → PSA-UPF. If the new link were listed first, slice `010203` would also
   be anchored directly at the PSA-UPF and the breakout would be lost without
   any error.

4. **Announce the edge service only in the slice with breakout**, in
   `manifests/apps/mec-af.yaml`:

   ```yaml
   - name: SNSSAIS
     value: "1:010203"
   ```

5. **Add the UEs of the second slice**: a second UERANSIM pod whose gNB and UE
   use SD `0x112233`, with a different gNB identity (for example
   `nci: '0x000000020'`), and subscribers provisioned in that slice
   (`provision-subscribers.py --sd 112233 --imsi-base <other range>`). These
   subscribers are not added to the UE routing file.

In scenario A no change is needed: the central UPF serves both slices.

The path of each slice is confirmed with the counters of section 9.6: traffic
of the second slice increases the counter of the PSA-UPF and leaves both the
counter and the pod interface of the I-UPF unchanged.

## 11.5 Breakout without the NEF

The static rule of section 5.7 removes the NEF and the Application Function
from the picture. The NEF manifest and `apps/mec-af.yaml` are then not
required, and `specificPath` must be regenerated and the SMF restarted whenever
the address of the edge Service changes.

## 11.6 Monitoring

The network functions of v4.x export Prometheus metrics. In the manifests the
`metrics` block is enabled on port 9091 and each Service exposes that port.
Among the exported series, `free5gc_sbi_inbound_request_duration_seconds` is a
histogram of the duration of the requests received on the service-based
interface, which gives the signalling latency per function.

Where the Prometheus Operator is installed, the scrape configuration is
created with:

```bash
kubectl apply -f manifests/monitoring/servicemonitors.yaml
```

The label `release: prometheus` under `metadata.labels` of each `ServiceMonitor`
must match the `serviceMonitorSelector` of the Prometheus instance of the
cluster; an object whose label does not match is ignored without any error.

The UPF of free5GC does not export Prometheus metrics. Its activity is
observed through the counters of the `upfgtp` interface (section 9.6).

A service mesh that handles only TCP does not observe the user plane or its
control: GTP-U and PFCP are carried over UDP, and N2 over SCTP.
