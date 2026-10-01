# 8. Deployment

This document gives the complete procedure, first with the scripts and then
step by step with `kubectl`. All commands are run from the root of the
repository.

## 8.1 Preparation

1. Load gtp5g on the nodes that will host a UPF (section 2.3).
2. Adjust the cluster-specific values:
   - the storage class in `manifests/common/mongo.yaml` (section 2.2);
   - the DNS address in the `dns` blocks of both SMF manifests (section 2.5).
3. Install the Python dependencies:

   ```bash
   pip install -r scripts/requirements.txt
   ```

4. Assign the nodes (section 2.4):

   ```bash
   python3 scripts/set-nodes.py --core <node> --edge <node> --ran <node> --remote <node>
   ```

5. Validate the manifests:

   ```bash
   python3 scripts/validate.py
   ```

   The validator reports configuration errors that the network functions would
   only reveal at start-up or, in some cases, not at all: schema versions,
   unroutable endpoints, overlapping UE pools, a UPF `nodeID` that does not
   match the SMF, a missing `ulcl: true`, node names in the UE routing file that
   do not exist in the topology, an announced DNAI that no UPF declares, and
   generated ConfigMaps that are older than their sources.

## 8.2 Deployment with the scripts

```bash
python3 scripts/deploy.py up b
```

The script performs, in order:

1. creation of the namespace, if it does not exist;
2. removal of the user plane of the other scenario, if present;
3. the control plane, one function at a time, waiting for each rollout and for
   the registration of the function at the NRF (section 3.4);
4. the user plane of the chosen scenario, UPFs first and SMF last;
5. the applications and the Application Function.

The SMF and the Application Function are restarted after their manifests are
applied, because both read their configuration or code from a ConfigMap only
at start-up; without the restart, a pod that already existed would keep the
previous version.

Then the subscribers are provisioned and the RAN is started:

```bash
kubectl -n free5gc port-forward svc/mongo 27017:27017 &
python3 scripts/provision-subscribers.py --count 5 --clean
kill %1

python3 scripts/deploy.py ran
```

The state of the deployment is summarized by:

```bash
python3 scripts/deploy.py status
```

which lists the pods and Services, the functions that have not confirmed their
registration at the NRF, the UPFs with a recent PFCP heartbeat, and the last
log lines of the Application Function.

Options of `deploy.py`:

| Option | Default | Meaning |
| --- | --- | --- |
| `--delay` | 10 | Pause between one network function and the next, in seconds |
| `--timeout` | 180 | Time allowed for each function to become ready and to register, in seconds |

## 8.3 Manual deployment

The same procedure with `kubectl`, for scenario B. Each step should be
confirmed before the next one.

**Namespace**

```bash
kubectl apply -f manifests/namespace.yaml
```

**MongoDB and NRF**

```bash
kubectl apply -f manifests/common/mongo.yaml
kubectl -n free5gc rollout status deploy/free5gc-mongodb

kubectl apply -f manifests/common/nrf.yaml
kubectl -n free5gc rollout status deploy/free5gc-nrf
kubectl -n free5gc logs deploy/free5gc-nrf | grep "SBI server started"
```

A pause of about ten seconds after the NRF reports that its server started
avoids the race described in section 3.4.

**Remaining control-plane functions**, in this order: `udr`, `udm`, `ausf`,
`nssf`, `pcf`, `nef`, `amf`, `webui`. For each of them:

```bash
kubectl apply -f manifests/common/udr.yaml
kubectl -n free5gc rollout status deploy/free5gc-udr
kubectl -n free5gc logs deploy/free5gc-udr | grep -e "OAuth2 setting receive from NRF" -e "register to NRF Error"
```

The function is registered when the `OAuth2 setting receive from NRF` line is
the last of the two. Otherwise:

```bash
kubectl -n free5gc rollout restart deploy/free5gc-udr
```

**User plane**

```bash
kubectl apply -f manifests/scenario-b-edge-upf/i-upf.yaml
kubectl apply -f manifests/scenario-b-edge-upf/psa-upf.yaml
kubectl -n free5gc rollout status deploy/free5gc-i-upf
kubectl -n free5gc rollout status deploy/free5gc-psa-upf

kubectl apply -f manifests/scenario-b-edge-upf/smf.yaml
kubectl -n free5gc rollout status deploy/free5gc-smf
```

**Applications and Application Function**

```bash
kubectl apply -f manifests/apps/code-configmaps.yaml
kubectl apply -f manifests/apps/apps.yaml
kubectl apply -f manifests/apps/mec-af.yaml
kubectl -n free5gc rollout status deploy/mec-af
kubectl -n free5gc logs deploy/mec-af
```

The log of the Application Function must show one `POST ok` line per slice
(section 6.7).

**Subscribers and RAN**

```bash
kubectl -n free5gc port-forward svc/mongo 27017:27017 &
python3 scripts/provision-subscribers.py --count 5 --clean
kill %1

kubectl apply -f manifests/ran/ueransim.yaml
kubectl -n free5gc rollout status statefulset/free5gc-ueransim
```

## 8.4 Switching between scenarios

```bash
python3 scripts/deploy.py up a
```

The command removes the UPFs and the SMF of scenario B, applies those of
scenario A, and leaves the control plane, the applications and the subscribers
in place. The two scenarios must not be deployed at the same time: both define
a Deployment named `free5gc-smf`, and a UPF left over from the other scenario
would not be part of the topology known to the SMF.

After the user plane is replaced, the PDU session of the UE is no longer
valid, although UERANSIM still reports the UE as registered (section 7.3). The
RAN pod is recreated before any verification:

```bash
kubectl -n free5gc delete pod free5gc-ueransim-0
```

## 8.5 Applying a configuration change

| Change | Action |
| --- | --- |
| Configuration of a control-plane function | `kubectl apply -f <manifest>` and `kubectl -n free5gc rollout restart deploy/free5gc-<nf>` |
| SMF configuration or UE routing file | `kubectl apply -f <manifest>`, `kubectl -n free5gc rollout restart deploy/free5gc-smf`, then recreate the UERANSIM pod |
| UPF configuration | `kubectl apply -f <manifest>`, restart the UPF, then restart the SMF so that the PFCP association is established again, then recreate the UERANSIM pod |
| Environment variables of the Application Function | `kubectl apply -f manifests/apps/mec-af.yaml` (the pod is recreated) |
| `af/mec_af.py` or `probe/probe.py` | `python3 scripts/gen-configmaps.py`, `kubectl apply -f manifests/apps/code-configmaps.yaml`, then restart the pods that mount the code |
| Edge Service recreated (new ClusterIP) | None: the Application Function updates the rule at its next cycle |

## 8.6 Removal

```bash
python3 scripts/deploy.py down
```

All resources created by the manifests are deleted, including the persistent
volume claim of MongoDB, and with it the provisioned subscribers. The
namespace is kept.

Next: [09-verification.md](09-verification.md).
