# 10. Troubleshooting

Most of the failures below are silent: the pods are `Running`, the logs show no
error at the point of failure, and the symptom appears in a different
component. The entries are grouped by the stage at which the symptom is
observed.

## 10.1 Start-up of the network functions

| Symptom | Cause | Correction |
| --- | --- | --- |
| A function logs `register to NRF Error ... context deadline exceeded` | It started before the NRF could write to MongoDB (section 3.4) | Restart the function once the NRF is ready: `kubectl -n free5gc rollout restart deploy/free5gc-<nf>` |
| SMF exits with `overlap cidr value between UPFs` | The same UE pool is declared for two slices; pools are validated globally (section 5.3) | Use disjoint pools per slice |
| SMF exits with `open ./config/uerouting.yaml: no such file or directory` | The `-u` option is missing; the file is read even without ULCL (section 5.6) | Pass `-u` with a UE routing file, empty in scenario A |
| Web console exits with `invalid BillingServer.Enable: non zero value required` | `billingServer.enable: false` (section 3.3) | Set it to `true` |
| A function rejects its configuration at start-up after a version change | `info.version` or a required field does not match the release (section 2.1) | Compare with the example configuration of that release; run `scripts/validate.py` |
| A pod stays `Pending` | The `nodeSelector` names a node that does not exist, or a placeholder was left | `kubectl -n free5gc describe pod <pod>`; fix the hostname |
| MongoDB pod stays `Pending` | The storage class of the volume claim does not exist in the cluster (section 2.2) | Replace `storageClassName` |
| A configuration change has no effect | The pod was not restarted after the ConfigMap changed (section 3.1) | `kubectl -n free5gc rollout restart deploy/<name>` |
| `kubectl apply` fails with `no matches for kind "ServiceMonitor"` | The Prometheus Operator is not installed | Do not apply `manifests/monitoring/` |

## 10.2 PFCP association

| Symptom | Cause | Correction |
| --- | --- | --- |
| No PFCP heartbeat in a UPF log; SMF and UPF report no error | `pfcp.nodeID` of the UPF differs from the `nodeID` in the SMF configuration (section 4.1) | Make them equal; `scripts/validate.py` detects the mismatch |
| Only one association in scenario B | The SMF is running with the configuration of scenario A | Apply `scenario-b-edge-upf/smf.yaml` and restart the SMF |
| SMF logs `Sequence number ... already exist`, then `UPF[...] not associated with SMF` and `retry-out` | Usage reports flooding the SMF (section 4.6) | Raise the URR thresholds; restart the affected **UPF**, then the SMF |
| The association is not re-established after an SMF restart | The UPF is refusing the association after a previous failure (section 4.6) | Restart the UPF |

## 10.3 UE registration

| Symptom | Cause | Correction |
| --- | --- | --- |
| UE logs `Initial Registration failed [CONGESTION]`; UDM logs `Mandatory IE [ausfInstanceId] is missing or invalid` or `Mandatory IE [AmfInstanceId] is missing or invalid` | The AUSF or the AMF holds an empty instance identifier (section 3.4) | Restart the function named by the missing identifier, then recreate the UERANSIM pod |
| UE never registers; the core was deployed correctly | UERANSIM started before the subscribers were provisioned and the UE stopped retrying (section 7.3) | `kubectl -n free5gc delete pod free5gc-ueransim-0` |
| Registration rejected for an unknown subscriber | The IMSI is not provisioned, or the PLMN or key differs between the UE and the database | Provision the subscriber; compare `ue.yaml` with the options of the provisioning script |
| Provisioning script stops with `tenantData is empty` | The web console has not started yet (section 7.1) | Wait for the web console to be ready and run the script again |
| Provisioning script fails with a `ConfigurationError` about the server version | `pymongo` 4.14 or newer (section 2.6) | Install the version range of `scripts/requirements.txt` |
| gNB does not complete NG Setup | The AMF Service is not reachable over SCTP, or PLMN, TAC or slice differ between the gNB and the AMF | Check SCTP support in the cluster (section 2.2) and the identifiers |
| The UE reports `RM-REGISTERED` but no traffic passes after a redeployment | UERANSIM keeps its state after the core is replaced (section 7.3) | Recreate the UERANSIM pod |

## 10.4 PDU session and data path

| Symptom | Cause | Correction |
| --- | --- | --- |
| The interface `upfgtp` does not exist in the UPF pod | gtp5g is not loaded on the node (section 2.3) | Build and load the module on that node; restart the UPF |
| `upfgtp` RX increases, TX stays at zero; nothing returns to the UE; no log | `ip_forward` is 0 in the UPF container (section 4.2) | Keep the `echo 1 > /proc/sys/net/ipv4/ip_forward` line of the start-up script |
| Uplink reaches the application, no reply reaches the UE | The `MASQUERADE` rule is missing on the UPF that hands the traffic to the data network (section 4.2) | Restore the rule, in scenario B on the I-UPF as well |
| Traffic to the edge application works in scenario B only in the uplink direction | The UE pool is missing from `dnnList` of the I-UPF (section 4.1) | List every pool that traverses the UPF |
| `PDU Session Establishment` fails with `no response from the network`, or the UE stays registered without `uesimtun0` | The PFCP association with a UPF was lost (section 4.6) | See section 10.2 |
| Latency is very low and both destinations look identical | The traffic is not crossing the 5G user plane: the socket is not bound to the UE address or the destination is not routed through `uesimtun0` (section 7.2) | Bind to the UE address and install the routes |
| The data path stops in the middle of a run with sustained traffic | Usage reports (section 4.6) | Raise the URR thresholds |
| Packet captures at the gNB show downlink GTP-U from an unexpected address | The source is the ClusterIP of the UPF Service (section 4.4) | Expected behaviour |

## 10.5 Local breakout

| Symptom | Cause | Correction |
| --- | --- | --- |
| The PSA-UPF counts the traffic sent to the edge application | No breakout rule is installed | Check, in order: `ulcl: true` in the SMF configuration; the subscriber in `members`; the Application Function log; the influence data in the UDR (section 6.7) |
| Application Function logs `NEF not answering yet` continuously | The NEF is not running or not registered | Check the NEF pod and its registration at the NRF |
| Application Function logs `POST -> 4xx/5xx` | The NEF rejected the subscription or could not write to the UDR | Read the NEF log; check that the UDR and the PCF are registered |
| Application Function logs `no service resolves` | The Service named in `SERVICES` does not exist in the namespace given by `NS` | Create the Service or correct the name |
| The rule exists in the UDR but the traffic is not diverted | The DNAI of the subscription is not declared for the I-UPF in the slice of the session (section 5.2) | Declare the DNAI in `sNssaiUpfInfos` of the I-UPF and restart the SMF |
| The rule still points to the previous address after the Service was recreated | Less than one polling interval has elapsed, or the Application Function is not running | Wait `POLL_S` seconds; check the pod |
| Sessions carry duplicate rules for the same destination | Orphan influence data in the UDR after a NEF restart, or more than one client using the same `AF_ID` (section 6.6) | Let the Application Function run its clean-up; use one `AF_ID` per client |
| A change in `af/mec_af.py` has no effect | The ConfigMap was not regenerated, or the pod was not restarted (section 8.5) | `python3 scripts/gen-configmaps.py`, apply, restart |
| The remote application is also reached through the edge | `remote-app` was added to `SERVICES`, or a static rule covers its address | Remove it from `SERVICES`; narrow the `dest` of the static rule |
| The breakout works with the static rule but not with Traffic Influence | The fault is in the chain AF → NEF → UDR → PCF → SMF (section 5.7) | Inspect each element with the commands of section 6.7 |

## 10.6 Collecting information

```bash
python3 scripts/deploy.py status

kubectl -n free5gc logs deploy/free5gc-smf --tail=200
kubectl -n free5gc logs deploy/free5gc-i-upf --tail=100
kubectl -n free5gc logs deploy/free5gc-psa-upf --tail=100
kubectl -n free5gc logs deploy/mec-af --tail=50
kubectl -n free5gc logs free5gc-ueransim-0 -c gnb --tail=50
kubectl -n free5gc logs free5gc-ueransim-0 -c ue --tail=50

kubectl -n free5gc exec deploy/free5gc-i-upf -- cat /proc/net/dev
kubectl -n free5gc exec deploy/free5gc-i-upf -- cat /proc/sys/net/ipv4/ip_forward
kubectl -n free5gc exec deploy/free5gc-i-upf -- grep gtp5g /proc/modules
```

The log of a network function is rotated by the cluster; under sustained
traffic the lines printed at start-up may no longer be available. Checks that
depend on a start-up line should be replaced by checks of the current state,
such as the PFCP heartbeat.

Next: [11-extensions.md](11-extensions.md).
