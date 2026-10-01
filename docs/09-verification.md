# 9. Verification

A working session and a lower latency towards the edge application do not
demonstrate a local breakout. The same observations are obtained when the edge
application is simply closer to the UE while the session remains anchored in
the core, or when the traffic does not cross the 5G user plane at all. The
verification therefore observes the user plane directly.

The checks are layered: each one depends on the previous ones, and a failure
at one layer produces misleading symptoms at the next. `scripts/verify-lbo.py`
runs the checks of sections 9.4 to 9.7; the others are run by hand or by
`scripts/deploy.py status`.

## 9.1 Registration of the network functions

```bash
python3 scripts/deploy.py status
```

No function may appear in the list of functions that have not confirmed their
registration at the NRF (section 3.4).

## 9.2 PFCP associations

The SMF must be associated with one UPF in scenario A and with two in scenario
B. An association is a state, not a start-up event: the line that records it
is printed once, when it is established, and may no longer be in the log after
some minutes of traffic. The periodic PFCP heartbeat is the reliable indicator
that an association is alive at the moment of the check:

```bash
kubectl -n free5gc logs deploy/free5gc-i-upf --since=40s | grep -i heartbeat
kubectl -n free5gc logs deploy/free5gc-psa-upf --since=40s | grep -i heartbeat
```

A UPF without heartbeats is not associated. The usual causes are a `nodeID`
that differs between the UPF and the SMF configuration (section 4.1) and an
association lost under load (section 4.6).

## 9.3 gNB and UE

```bash
kubectl -n free5gc logs free5gc-ueransim-0 -c gnb | grep "NG Setup procedure is successful"
kubectl -n free5gc exec free5gc-ueransim-0 -c ue -- ./nr-cli imsi-208930000000000 -e status
kubectl -n free5gc exec free5gc-ueransim-0 -c ue -- ip -4 -o addr show uesimtun0
```

Expected: the NG Setup line, the state `RM-REGISTERED`, and an address in
`60.60.0.0/17` on `uesimtun0`. In scenario B the address is allocated by the
PSA-UPF, so its presence shows that the default path through the I-UPF and N9
was established.

## 9.4 Announcement of the edge service (scenario B)

```bash
kubectl -n free5gc logs deploy/mec-af | grep "POST ok"
kubectl -n free5gc get svc mec-app -o jsonpath='{.spec.clusterIP}'
```

The address in the `POST ok` lines must be the current ClusterIP of `mec-app`.

## 9.5 Routes through the PDU session

The destinations must be routed through the tunnel interface of the UE, and
the traffic must be sent from the UE address (section 7.2):

```bash
kubectl -n free5gc exec free5gc-ueransim-0 -c ue -- ip route replace <mec-app ClusterIP>/32 dev uesimtun0
kubectl -n free5gc exec free5gc-ueransim-0 -c ue -- ip route replace <remote-app ClusterIP>/32 dev uesimtun0
```

## 9.6 Counter-based verification of the breakout

gtp5g creates a network interface named `upfgtp` inside each UPF pod. Its
packet counters, read from `/proc/net/dev`, show which UPF terminated the
tunnel for a given flow:

```bash
kubectl -n free5gc exec deploy/free5gc-i-upf -- cat /proc/net/dev | grep upfgtp
kubectl -n free5gc exec deploy/free5gc-psa-upf -- cat /proc/net/dev | grep upfgtp
```

The receive counter increases for uplink packets that the UPF decapsulates,
and the transmit counter for downlink packets that it encapsulates. A UPF that
only relays a tunnel from N3 to N9, without terminating it, does not count the
relayed packets on this interface.

The procedure is:

1. read the counters of every UPF;
2. send a known number of packets from the UE to the **edge** application;
3. read the counters again and compute the difference;
4. repeat steps 1 to 3 with the **remote** application.

Expected result in **scenario B**, for N packets sent to each destination:

| Destination | I-UPF `upfgtp` RX | PSA-UPF `upfgtp` RX |
| --- | --- | --- |
| Edge application | about N | **0** |
| Remote application | 0 | about N |

- The PSA-UPF column is the decisive one. It must stay unchanged during the
  edge run and increase during the remote run. The contrast between the two
  runs is the evidence of the breakout.
- The I-UPF counts the edge traffic, which it decapsulates and sends to the
  application. During the remote run its counter stays at zero: it relays the
  tunnel to N9 without terminating it. A zero in that cell is expected and is
  not a failure.
- If the PSA-UPF counts both runs, the classifier is not diverting anything and
  the edge traffic is reaching the core.

In the reference testbed the count at the PSA-UPF during the edge run was zero
in every execution of this verification.

Expected result in **scenario A**: the single UPF counts both runs with about
N packets each. If it does not count the edge run, a user plane of scenario B
is still deployed.

## 9.7 Automated verification

```bash
python3 scripts/verify-lbo.py
```

The script detects the deployed scenario, waits for the UE registration and
for the tunnel address, reads the ClusterIPs of both applications, checks the
subscriptions held by the Application Function, installs the routes, and
performs the procedure of section 9.6 with the UDP echo probe
(`probe/probe.py`): 500 datagrams of 200 bytes, one every 10 ms, to each
destination.

Example of the output in scenario B (addresses and times vary):

```
[1] Scenario
    ok  scenario B (free5gc-i-upf, free5gc-psa-upf)

[2] UE registration and PDU session
    ok  imsi-208930000000000 is RM-REGISTERED
    ok  uesimtun0 = 60.60.0.1 (allocated by the anchor UPF)

[3] Destinations
    ok  mec-app = 10.96.31.7, remote-app = 10.96.202.15
    ok  the Application Function holds 2 subscription(s) for 10.96.31.7

[4] Routes through the PDU session
    ok  10.96.31.7 and 10.96.202.15 routed through uesimtun0

[5] Traffic and UPF counters
    sending 500 datagrams to 10.96.31.7 (edge)
    sending 500 datagrams to 10.96.202.15 (remote)

    Packets on the GTP-U interface of each UPF during each run (rx = uplink, tx = downlink):

    destination         i-upf rx      i-upf tx    psa-upf rx    psa-upf tx
    edge                     500           500             0             0
    remote                     0             0           500           500

    (table with packets sent and received, loss and round-trip time per destination)

[6] Verdict
    ok  LOCAL BREAKOUT CONFIRMED: the PSA-UPF counted 0 packets during the edge run and 500 during the remote run
```

The script exits with status 0 when the expected behaviour of the scenario is
confirmed and with status 1 otherwise, printing the most probable causes.

| Option | Default | Meaning |
| --- | --- | --- |
| `--count` | 500 | Datagrams sent to each destination |
| `--interval-ms` | 10 | Interval between datagrams |
| `--pod` | `free5gc-ueransim-0` | UERANSIM pod |
| `--tun` | `uesimtun0` | Tunnel interface of the UE |
| `--imsi` | `imsi-208930000000000` | IMSI of the UE |
| `--timeout` | 120 | Time allowed for the registration and the session, in seconds |

The breakout is declared active when the packets counted by the PSA-UPF during
the edge run are less than 2 % of those counted during the remote run. The
tolerance exists only to absorb unrelated packets that may cross the anchor
during the window; with the configuration of this repository the expected
value is zero.

## 9.8 Limits of the probe

The probe exists to place a known number of packets on a chosen path. The
round-trip times it prints are measured in user space, with a small number of
samples and without control of the sources of error, and are indicative only.

A latency comparison between the two scenarios requires, at least: a workload
generator that timestamps packets in the kernel and reports its own health;
repeated, independent deployments of each scenario; alternation of the order in
which the paths are measured; and a calibration of the resolution of the
instrument on the nodes in use. In an environment where the edge and the core
are in the same building, the propagation delay of the transport network is
negligible, and any difference between the scenarios comes from the user-plane
path itself (number of UPFs, tunnel terminations and hosts traversed), not from
distance.

Next: [10-troubleshooting.md](10-troubleshooting.md).
