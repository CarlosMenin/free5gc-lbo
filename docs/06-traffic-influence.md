# 6. Dynamic breakout rule through Traffic Influence

In the default configuration of this repository the SMF configuration contains
no address of the edge application. The breakout rule is created at runtime by
an Application Function (AF) through the Traffic Influence procedure.

## 6.1 Motivation

The breakout rule needs the address of the edge application. In Kubernetes
that address is the ClusterIP of a Service: it is allocated when the Service is
created and changes when the Service is recreated. A static rule
([05-ulcl-configuration.md](05-ulcl-configuration.md), section 5.7) has to
contain that address as a literal, and the SMF reads it only at start-up.

With Traffic Influence the address becomes runtime data. The AF discovers it,
announces it to the core, and the core installs the rule in established and in
new sessions, without restarting any network function.

## 6.2 Procedure

```
 mec-af              NEF            UDR            PCF            SMF          I-UPF
   │  DNS: mec-app    │              │              │              │             │
   │  → ClusterIP     │              │              │              │             │
   │── POST ─────────►│              │              │              │             │
   │  subscription    │── PUT ──────►│              │              │             │
   │                  │ influenceData│── notify ───►│              │             │
   │◄── 201 ──────────│              │              │── SM policy ►│             │
   │                  │              │              │   update     │── PFCP ────►│
   │                  │              │              │              │ session mod.│
```

1. The AF resolves the name of the edge Service and sends a Traffic Influence
   subscription to the NEF.
2. The NEF stores it in the UDR as application data (`influenceData`).
3. The PCF reads the influence data when it creates the policy of a PDU
   session, and subscribes to changes of that data for each session. When data
   is added or modified, the UDR notifies the PCF.
4. The PCF sends a policy update to the SMF, carrying a PCC rule with the
   traffic filter and the DNAI.
5. The SMF selects the UPF that declares the DNAI (the I-UPF, section 5.2) and
   installs the rule with a PFCP Session Modification.

Sessions that are established when the subscription is created receive the
rule through steps 3 to 5. Sessions created later receive it when the PCF
creates their policy.

## 6.3 NEF

The NEF (`manifests/common/nef.yaml`) needs no setting beyond the list of
exposed services:

```yaml
serviceList:
  - serviceName: 3gpp-traffic-influence
  - serviceName: nnef-oam
  - serviceName: nnef-callback
```

It is started after the UDR and the PCF, which it depends on.

## 6.4 Content of a subscription

The API is `Nnef_TrafficInfluence` (3GPP TS 29.522). A subscription is created
with

```
POST http://nef.free5gc.svc.cluster.local:8000/3gpp-traffic-influence/v1/{afId}/subscriptions
```

and the following body, in which `10.96.31.7` stands for the ClusterIP of the
edge Service:

```json
{
  "afServiceId": "mec-app",
  "dnn": "internet",
  "snssai": { "sst": 1, "sd": "010203" },
  "anyUeInd": true,
  "notificationDestination": "http://mec-af.free5gc.svc.cluster.local:8080/notify",
  "trafficFilters": [
    {
      "flowId": 1,
      "flowDescriptions": [ "permit out ip from 10.96.31.7/32 to 60.60.0.0/16" ]
    }
  ],
  "trafficRoutes": [ { "dnai": "mec" } ]
}
```

| Field | Meaning |
| --- | --- |
| `afServiceId` | Identifier chosen by the AF; the Service name is used. |
| `dnn`, `snssai` | Sessions to which the rule applies. One subscription is created per slice. |
| `anyUeInd` | The rule applies to any UE of that DNN and slice. |
| `notificationDestination` | Where the NEF sends event notifications. The AF only logs them. |
| `trafficFilters` | The flows to divert. The description is written in the downlink direction (`out`: from the service to the UE pool); the UPF applies it in both directions. |
| `trafficRoutes` | Where to route the matching traffic: the DNAI declared for the I-UPF. |

The UE pool in the flow description (`60.60.0.0/16`) is the union of the pools
declared for the PSA-UPF.

The scope of the rule is defined by the subscription (`anyUeInd`, DNN and
slice), not by the `members` list of the UE routing file (section 5.5). In the
configuration of this repository every UE is a member of the group, so the two
scopes coincide. An analysis of the SMF source indicates that the session of a
UE outside the group, in a slice for which the I-UPF declares the DNAI, receives
the rule as well; that case was not exercised on the reference testbed. A way
to keep selected UEs away from the edge UPF is described in
[11-extensions.md](11-extensions.md), section 11.4.

## 6.5 The Application Function

The AF of this repository is `af/mec_af.py`, deployed by
`manifests/apps/mec-af.yaml`. It uses only the Python standard library and runs
in a stock `python:3.12-slim` container; its source is mounted from a ConfigMap
generated by `scripts/gen-configmaps.py`.

It is placed on the edge node. An AF is not a function of the 5G core: it
belongs to the application domain and reaches the core through the NEF, as a
third-party function would.

### Operation

Every `POLL_S` seconds the AF performs one reconciliation cycle:

1. **Resolve.** Each name in `SERVICES` is resolved through DNS. The A record
   of a Service is its current ClusterIP. A name that does not resolve is
   treated as a Service that does not exist.
2. **Adopt.** The subscriptions currently registered under its `AF_ID` are read
   from the NEF and taken as the current state. Subscriptions that do not
   correspond to a service in `SERVICES` are deleted.
3. **Clean the UDR** (only when a subscription has to be created). Influence
   data records that carry the signature of this AF and do not belong to a
   live subscription are deleted (see section 6.6).
4. **Ensure.** For each service and slice: if no subscription exists, one is
   created (`POST`); if the address changed, the subscription is updated
   (`PUT`); if the service no longer resolves, its subscriptions are removed
   (`DELETE`).

### Configuration

| Variable | Default | Meaning |
| --- | --- | --- |
| `NS` | `free5gc` | Namespace used to build the FQDN of a short service name |
| `SERVICES` | `mec-app` | Edge services to announce, comma separated. A short name becomes `<name>.<NS>.svc.cluster.local`; an FQDN or an IPv4 address is used as given |
| `NEF_URL` | `http://nef.free5gc.svc.cluster.local:8000` | Base URL of the NEF |
| `UDR_URL` | `http://udr.free5gc.svc.cluster.local:8000` | Base URL of the UDR, used only for the clean-up of step 3 |
| `AF_ID` | `af001` | AF identifier in the NEF URL; must be used by this AF only |
| `DNN` | `internet` | DNN of the target sessions |
| `SNSSAIS` | `1:010203,1:112233` | Slices, as `sst:sd`, comma separated |
| `UE_CIDR` | `60.60.0.0/16` | UE pool used in the flow description |
| `DNAI` | `mec` | DNAI of the edge domain; equal to the `dnaiList` of the I-UPF |
| `NOTIFY_URL` | `http://mec-af.free5gc.svc.cluster.local:8080/notify` | Notification destination given to the NEF |
| `POLL_S` | `10` | Interval between reconciliation cycles, in seconds |
| `HTTP_PORT` | `8080` | Port of the notification and status server |

`remote-app` must not be added to `SERVICES`: it is the reference path, and
announcing it would divert it to the edge as well.

### Design decisions

**Discovery through DNS instead of the Kubernetes API.** The AF needs no
ServiceAccount, Role or RoleBinding, and no credential is mounted in its
container. It relies on the same source that the SMF configuration already
uses to reach the UPFs. The cost is that a change is noticed at the next poll
rather than immediately, and that services cannot be selected by label. Since
a ClusterIP only changes when a Service is recreated, a ten-second poll is
sufficient.

**Adoption instead of recreation.** At every cycle the AF takes over the
subscriptions that already exist under its identifier. A restart of the AF is
therefore idempotent. Without adoption, each restart would add a duplicate
subscription, and each duplicate becomes a duplicate rule in every session.

**No deletion on termination.** The AF does not remove its subscriptions when
it receives `SIGTERM`. The rule lives in the core; a restart of the component
that announced it must not withdraw the breakout from sessions in use. In
addition, during a rolling update the old and the new pod may overlap, and a
deletion by the old pod would remove what the new one has just adopted.

**`Recreate` strategy.** The AF is a singleton controller. Two instances
running at the same time would compete for the same subscriptions.

## 6.6 Behaviour of the free5GC NEF

The following behaviours of the NEF and UDR of v4.2.3 were observed during the
integration and are handled by the AF. They are relevant to any client of the
API.

| Behaviour | Consequence |
| --- | --- |
| The AF context is created by the first `POST`. Until then, `GET .../{afId}/subscriptions` answers `404 AF is not found`. | A `200` on the list request cannot be used as a readiness check for the NEF. Any HTTP response indicates that the NEF is reachable. |
| Subscriptions are kept in memory only. | A restarted NEF has no subscriptions; the AF recreates them at the next cycle. |
| The list response includes the field `self` with the subscription URI. | The subscription identifier is taken from `self` (or from the `Location` header of the `POST` response). |
| Each `POST` stores the influence data in the UDR under a new identifier. | After a NEF restart, the records of the previous subscriptions remain in the UDR as orphans. Each orphan becomes a duplicate rule in every session, which is why the AF removes them before creating a subscription. |
| The UDR path prefix is `/nudr-dr/v2`. | Examples that use `/nudr-dr/v1` receive `404`. The AF tries `v2` and then `v1`. |
| For a query with no result, the UDR answers `200` with the body `null`, not `[]`. | A client must accept `null` as an empty list. |

The clean-up in the UDR is the only direct interaction between the AF and the
UDR; everything else goes through the NEF. A record is recognised as belonging
to the AF when it applies to any UE, has the configured DNN and DNAI, and its
flow description has the exact format of section 6.4.

## 6.7 Inspecting the state

Log of the AF. A successful announcement produces a line with `POST ok`:

```bash
kubectl -n free5gc logs deploy/mec-af
```

```
DNS: mec-app=10.96.31.7
mec-app [1:010203] -> 10.96.31.7: POST ok (sub 1); SDF='permit out ip from 10.96.31.7/32 to 60.60.0.0/16' dnai=mec
mec-app [1:112233] -> 10.96.31.7: POST ok (sub 2); SDF='permit out ip from 10.96.31.7/32 to 60.60.0.0/16' dnai=mec
```

Current subscriptions, from the status endpoint of the AF:

```bash
kubectl -n free5gc exec deploy/mec-af -- python3 -c \
  "import urllib.request as u; print(u.urlopen('http://127.0.0.1:8080/status').read().decode())"
```

Influence data stored in the UDR:

```bash
kubectl -n free5gc exec deploy/mec-af -- python3 -c \
  "import urllib.request as u; print(u.urlopen('http://udr.free5gc.svc.cluster.local:8000/nudr-dr/v2/application-data/influenceData?dnns=internet').read().decode())"
```

Propagation to the SMF: after a subscription is created, the logs of the PCF
and of the SMF can be searched for entries related to the influence data and
to the policy update.

```bash
kubectl -n free5gc logs deploy/free5gc-pcf | grep -i influence
kubectl -n free5gc logs deploy/free5gc-smf | grep -i -e pcc -e influence
```

The effect on the data path is checked with the counters
([09-verification.md](09-verification.md)).

## 6.8 Using the API by hand

The API can be exercised without the AF, for example to test a rule for a
single destination. The commands below run from a temporary pod that provides
`curl`:

```bash
kubectl -n free5gc run nef-client --rm -it --restart=Never --image=curlimages/curl -- sh
```

Inside that pod:

```bash
NEF=http://nef.free5gc.svc.cluster.local:8000/3gpp-traffic-influence/v1/af001/subscriptions

# create
curl -s -i -X POST "$NEF" -H 'Content-Type: application/json' -d '{
  "afServiceId": "mec-app",
  "dnn": "internet",
  "snssai": {"sst": 1, "sd": "010203"},
  "anyUeInd": true,
  "notificationDestination": "http://mec-af.free5gc.svc.cluster.local:8080/notify",
  "trafficFilters": [{"flowId": 1, "flowDescriptions": ["permit out ip from 10.96.31.7/32 to 60.60.0.0/16"]}],
  "trafficRoutes": [{"dnai": "mec"}]
}'

# list (the identifier is the last element of "self")
curl -s "$NEF"

# delete
curl -s -i -X DELETE "$NEF/<id>"
```

When the AF is running under the same `AF_ID`, it removes at its next cycle
any subscription that does not correspond to its `SERVICES`. Manual tests
should use a different identifier (for example `af002`) or be done with the AF
stopped.

To remove every rule (for instance before testing the static rule of section
5.7), the AF is stopped first and its subscriptions are then deleted through
the NEF as shown above. The NEF then removes the corresponding record from the
UDR, which can be confirmed with the query of section 6.7.

Next: [07-ran-and-subscribers.md](07-ran-and-subscribers.md).
