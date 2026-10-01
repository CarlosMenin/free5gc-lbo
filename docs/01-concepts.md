# 1. Concepts

This document introduces the mechanisms used in the rest of the tutorial and
defines the two scenarios that the repository deploys.

## 1.1 Local breakout

In a 5G core, user traffic leaves the mobile network at a User Plane Function
(UPF) that acts as the **PDU Session Anchor (PSA)**: the point where the GTP-U
tunnel ends and packets are handed to a data network over the N6 interface. In
a centralized deployment the anchor is located in the core site, so every
packet of a UE travels from the access site to the core before reaching any
service, including a service hosted next to the access site.

**Local Breakout (LBO)** removes that detour for selected traffic. A UPF is
placed at the edge site, and the flows destined to edge services leave the
user plane there. Traffic to any other destination keeps its path to the
central anchor.

## 1.2 UPF roles in a session

3GPP TS 23.501 (clause 5.6.4) defines how one PDU session can use more than one
UPF. The roles relevant here are:

| Role | Function |
| --- | --- |
| PDU Session Anchor (PSA) | Terminates the session towards a data network (N6). Allocates, or is associated with, the UE address. |
| Uplink Classifier (UL CL) | An intermediate UPF that inspects uplink packets against traffic filters and forwards each one towards one of several anchors. Downlink traffic from those anchors is merged towards the UE. |
| Intermediate UPF (I-UPF) | Any UPF between the access network (N3) and an anchor (N9). |

With an Uplink Classifier, the session has a single IPv4 address and the UE is
unaware of the split: the classification is done by the network, on the
destination of each uplink packet.

In this repository the edge UPF combines two roles. It is the Uplink
Classifier of the session and, for the diverted traffic, it is also the local
anchor: the matched packets are decapsulated and sent to the edge application
through the N6 interface of that same UPF. It is referred to as **I-UPF**
throughout. The UPF in the core is the anchor of the default path and is
referred to as **PSA-UPF**; it owns the address pool from which UE addresses
are allocated.

## 1.3 The Uplink Classifier in free5GC

free5GC implements the Uplink Classifier in the SMF. Three elements of the SMF
configuration are involved, all described in
[05-ulcl-configuration.md](05-ulcl-configuration.md):

1. the option `ulcl: true`;
2. a user-plane topology (`userplaneInformation`) that declares the chain
   gNB → I-UPF → PSA-UPF;
3. a UE routing file that lists the subscribers handled by the classifier and
   their default path.

The rule that diverts the traffic can reach the SMF in two ways:

- **statically**, as a `specificPath` entry in the UE routing file, which maps a
  destination prefix to a list of UPFs. The file is read once, when the SMF
  starts;
- **dynamically**, as a policy rule that carries a traffic filter and a Data
  Network Access Identifier, delivered by the PCF as the result of a Traffic
  Influence request (section 1.4).

In both cases the SMF installs, over PFCP, a Packet Detection Rule at the I-UPF
that matches the uplink packets addressed to the edge service and forwards them
to the local N6 interface instead of the N9 tunnel.

## 1.4 DNAI and Traffic Influence

A **Data Network Access Identifier (DNAI)** is a label for a location where a
data network can be accessed. In the SMF configuration a DNAI is attached to a
UPF, which makes that UPF eligible to serve traffic that a policy rule directs
to that DNAI. This repository uses a single DNAI, `mec`, attached to the I-UPF.

**Traffic Influence** (3GPP TS 23.501 clause 5.6.7; API in TS 29.522) is the
standardized procedure by which an Application Function (AF) asks the network
to route selected traffic to a given DNAI. The request travels through the
following chain:

```
AF ──► NEF ──► UDR ──► PCF ──► SMF ──► I-UPF
   HTTP     stores    reads and     policy     PFCP session
   request  influence subscribes    update     modification
            data      to updates
```

The request contains a traffic filter (which flows), a route (which DNAI), and
a scope (which UEs, DNN and slice). The SMF resolves the DNAI to the UPF that
declares it and installs the corresponding rule. The rule is applied to
sessions that are already established as well as to new ones.

The dynamic path is the default in this repository, because the address of the
edge application is assigned by Kubernetes when its Service is created and
changes whenever the Service is recreated. With Traffic Influence that address
is runtime data announced by the AF; with a static rule it would be a literal
in the SMF configuration and each change would require restarting the SMF.
Details are in [06-traffic-influence.md](06-traffic-influence.md).

## 1.5 The two scenarios

**Scenario A (baseline).** One UPF, in the core, terminates N3 and anchors every
session. The SMF has no Uplink Classifier. Traffic to the edge application
reaches it through the N6 interface of the central UPF: it leaves the edge
site, crosses the transport network to the core, and crosses it again to return
to the edge.

**Scenario B (local breakout).** Two UPFs. The I-UPF, at the edge, terminates N3
and classifies uplink traffic; the PSA-UPF, in the core, anchors the default
path over N9. Traffic to the edge application is diverted at the I-UPF and does
not reach the core; traffic to any other destination follows the default path.

Everything else is identical in the two scenarios: control plane, subscribers,
applications, Application Function and RAN emulation. The only difference is
the user plane and the SMF configuration that describes it, which makes the two
scenarios directly comparable.

The remote application exists to show that the classifier is selective. A
configuration that sent all traffic to the edge would also shorten the path to
the edge application; only the contrast between the two destinations, within
the same session, demonstrates classification.

## 1.6 Reason for choosing free5GC

Among the open-source 5G cores commonly used in testbeds, free5GC provides a
native Uplink Classifier. In Open5GS an edge breakout is normally expressed by
selecting a different UPF per DNN or per slice, which anchors the whole session
at the edge: a single session cannot then reach a local destination through the
edge and a remote destination through the core. That is a different
architectural behaviour from the one described above.

## 1.7 Abbreviations

| Term | Meaning |
| --- | --- |
| AF | Application Function |
| DNAI | Data Network Access Identifier |
| DNN | Data Network Name |
| F-TEID | Fully Qualified Tunnel Endpoint Identifier |
| I-UPF | Intermediate UPF; here, the edge UPF acting as Uplink Classifier |
| LBO | Local Breakout |
| NEF | Network Exposure Function |
| PCC | Policy and Charging Control |
| PDR | Packet Detection Rule |
| PFCP | Packet Forwarding Control Protocol (N4) |
| PSA | PDU Session Anchor |
| SDF | Service Data Flow |
| S-NSSAI | Single Network Slice Selection Assistance Information (SST and SD) |
| ULCL | Uplink Classifier |
| URR | Usage Reporting Rule |

Next: [02-testbed-and-prerequisites.md](02-testbed-and-prerequisites.md).
