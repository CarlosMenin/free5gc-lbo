# 3. Control plane

The control plane is the same in both scenarios. Its manifests are in
`manifests/common/`, one file per network function. The SMF is the only
control-plane function whose configuration differs between scenarios; it is
described in [04-user-plane.md](04-user-plane.md) and
[05-ulcl-configuration.md](05-ulcl-configuration.md).

## 3.1 Structure of a manifest

Each file contains three objects:

| Object | Content |
| --- | --- |
| `ConfigMap` | The configuration file of the function, embedded as text (`<nf>Config.yaml`) |
| `Deployment` | One replica of the official image, started with `-c /etc/free5gc/<nf>Config.yaml` |
| `Service` | A ClusterIP Service named after the function (`amf`, `smf`, `nrf`, ...) |

The ConfigMap is mounted at `/etc/free5gc`. A change to a ConfigMap does not
restart the pod that mounts it: after editing a configuration, the Deployment
has to be restarted explicitly.

```bash
kubectl apply -f manifests/common/amf.yaml
kubectl -n free5gc rollout restart deploy/free5gc-amf
```

Each Deployment sets `spec.hostname` (`amf-pod`, `smf-pod`, ...). Some
configuration fields must contain an address of the pod itself and cannot be
`0.0.0.0`; the pod hostname, which resolves to the pod address through
`/etc/hosts`, is used there (for example `ngapIpList` in the AMF and
`pfcp.addr` in the UPF).

## 3.2 Addressing by service name

No pod address is written in any configuration file. Each function registers
at the NRF with the FQDN of its Service and reaches the NRF by name:

```yaml
sbi:
  scheme: http
  registerIPv4: amf.free5gc.svc.cluster.local   # advertised to other functions
  bindingIPv4: 0.0.0.0                          # listen address inside the pod
  port: 8000
nrfUri: http://nrf.free5gc.svc.cluster.local:8000
```

The same convention is used on the user plane: the SMF configuration refers to
the UPFs by Service FQDN (section 4.5). The topology description therefore does
not depend on the addresses that the cluster allocates.

The service-based interface uses plain HTTP. TLS key and certificate paths are
present in the files because the configuration schema requires them, but they
are not used with `scheme: http`.

## 3.3 Function-specific settings

Only the settings that differ from the example configuration distributed with
free5GC, or that are constrained by v4.2.3, are listed.

**NRF** (`nrf.yaml`)

- Stores the profiles of the registered functions in MongoDB
  (`MongoDBUrl: mongodb://mongo:27017`).
- `oauth: false`. free5GC v4 supports OAuth2 between network functions. It is
  disabled here because it requires distributing certificates to every
  function and adds token validation to each signalling exchange, which is an
  additional variable when signalling latency is measured. Enabling it requires
  `oauth: true` in the NRF, the NRF certificate configured in every function,
  and a Secret carrying the certificates.

**UDR** (`udr.yaml`)

- v4.2.3 requires `info.version: 1.1.0` and the field
  `dbConnectorType: mongodb`.
- Besides subscriber data, the UDR stores the Traffic Influence data written by
  the NEF ([06-traffic-influence.md](06-traffic-influence.md)).

**UDM** (`udm.yaml`)

- The `SuciProfile` keys are the test set of 3GPP TS 33.501 Annex C.4, as in
  the free5GC example. The home network public key configured in the UE
  (`manifests/ran/ueransim.yaml`) is the one of profile 1.

**AUSF** (`ausf.yaml`) and **NSSF** (`nssf.yaml`)

- No change other than the service names. The slices listed in the NSSF must
  be the same as those in the AMF and in the SMF.

**PCF** (`pcf.yaml`)

- Uses MongoDB directly (`mongodb.url`).
- Takes part in the dynamic breakout: it reads the Traffic Influence data from
  the UDR when it creates the policy of a session, subscribes to changes, and
  notifies the SMF.

**NEF** (`nef.yaml`)

- Exposes `3gpp-traffic-influence`, `nnef-oam` and `nnef-callback`.
- It is used only by the Application Function. If the NEF is unavailable the
  core keeps operating; what is lost is the ability to announce new rules.
  Rules already stored in the UDR remain valid.

**AMF** (`amf.yaml`)

- `ngapIpList` contains the pod hostname (`amf-pod`), not `0.0.0.0`.
- The Service exposes port 38412 with `protocol: SCTP`.
- `servedGuamiList`, `supportTaiList` and `plmnSupportList` carry the PLMN, the
  tracking area code and the two slices of section 2.5.

**Web console** (`webui.yaml`)

- v4.2.3 requires `info.version: 1.0.3` and the blocks `nrfUri`, `webServer`
  and `billingServer`.
- `billingServer.enable` must be `true`. The field is validated as required,
  and that validation rejects the value `false`: with `enable: false` the
  process exits with `invalid BillingServer.Enable: non zero value required`.
- The executable inside the image is named `webui`.
- The Service is of type NodePort (port 30500). With the upstream image the
  default credentials are `admin` / `free5gc`.
- On its first start the web console creates the `admin` tenant in MongoDB.
  The provisioning script reads the identifier of that tenant
  ([07-ran-and-subscribers.md](07-ran-and-subscribers.md)), so the web console
  must have started once before subscribers are provisioned.

**MongoDB** (`mongo.yaml`)

- The image provides mongod 4.0.5. See section 2.2 for the storage class and
  section 2.6 for the client library constraint.

**Callback services.** The AMF, SMF, PCF and NEF configurations of v4.x list an
additional service (`namf-callback`, `nsmf-callback`, `npcf-callback`,
`nnef-callback`). Configurations migrated from free5GC v3 need these entries.

**Metrics.** Every function that supports it has the `metrics` block enabled
(HTTP, port 9091, namespace `free5gc`) and its Service exposes that port. The
metrics port must differ from the SBI port.

## 3.4 Start-up order and registration at the NRF

Applying all manifests at once and waiting a fixed time is not reliable. The
cause is a race at start-up:

1. The NRF accepts TCP connections about one second after it starts, but it
   only answers registration requests once MongoDB is ready, because it writes
   each profile to the database.
2. A function that tries to register in that interval does not receive
   `connection refused`; the connection opens and the request times out
   (`context deadline exceeded`).
3. The function keeps running, in state `Running`, with an **empty NF instance
   identifier**, and reports nothing further.
4. The failure appears much later, during the registration of a UE, in a place
   that has no obvious relation to its cause:

   | Function without identifier | Error logged by the UDM |
   | --- | --- |
   | AUSF | `Mandatory IE [ausfInstanceId] is missing or invalid` |
   | AMF | `Mandatory IE [AmfInstanceId] is missing or invalid` |

   The UE only reports `Initial Registration failed [CONGESTION]`.

Two measures prevent this state.

**Dependency order.** The functions are started one at a time:

```
mongo → nrf → udr → udm → ausf → nssf → pcf → nef → amf → webui
        then the UPFs, then the SMF
```

The UPFs are started before the SMF because the SMF initiates the PFCP
association; it then finds the UPFs already listening.

**Registration gate.** Every function with a service-based interface prints the
line

```
OAuth2 setting receive from NRF:
```

at the moment the NRF answers its registration (the line is printed even with
OAuth disabled). A function is considered registered only when that line
appears *after* its last `register to NRF Error` line. Checking for the mere
presence of the two lines is not sufficient: it does not distinguish a function
that failed and then succeeded from one that only failed.

`scripts/deploy.py` implements both measures: it applies each manifest, waits
for the rollout, waits for the registration marker, and restarts the function
once if the marker does not appear. The manual correction for a function in the
empty-identifier state is the same:

```bash
kubectl -n free5gc rollout restart deploy/free5gc-ausf
```

The registration state of all functions is shown by
`python3 scripts/deploy.py status`.

## 3.5 Changing identifiers

The identifiers of section 2.5 are repeated in several files. When one is
changed, every occurrence must be changed.

| Identifier | Files |
| --- | --- |
| PLMN (MCC/MNC) | `amf.yaml`, `ausf.yaml`, `nrf.yaml`, `nssf.yaml`, both `smf.yaml`, `ran/ueransim.yaml` (gNB, UE and `supi`), UE routing members, `--plmn` and `--imsi-base` of the provisioning script |
| Slices | `amf.yaml`, `nssf.yaml`, both `smf.yaml` (`snssaiInfos` and `sNssaiUpfInfos`), `ran/ueransim.yaml`, `SNSSAIS` in `apps/mec-af.yaml`, `--sst` and `--sd` of the provisioning script |
| DNN | `amf.yaml` (`supportDnnList`), both `smf.yaml`, all UPF files (`dnnList`), `ran/ueransim.yaml` (`apn`), `DNN` in `apps/mec-af.yaml`, `--dnn` of the provisioning script |
| UE address pool | both `smf.yaml` (`pools`), all UPF files (`dnnList.cidr`), `UE_CIDR` in `apps/mec-af.yaml` |
| Tracking area code | `amf.yaml`, `nssf.yaml`, `ran/ueransim.yaml` (gNB) |
| DNAI | `scenario-b-edge-upf/smf.yaml` (`snssaiInfos` and the `sNssaiUpfInfos` of the I-UPF), `DNAI` in `apps/mec-af.yaml` |

Next: [04-user-plane.md](04-user-plane.md).
