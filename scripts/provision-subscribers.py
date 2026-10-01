#!/usr/bin/env python3
"""
Provisions subscribers directly in the free5GC MongoDB.

free5GC has no single "subscribers" collection: each UE is represented by
eight documents, one in each collection of ALL_COLLECTIONS. The documents
below reproduce what the v4.2.3 web console writes when a subscriber is
created through its API. See docs/07-ran-and-subscribers.md.

A single DNN ("internet") is provisioned. The local breakout is decided per
destination by the Uplink Classifier inside that session, not by a dedicated
DNN.

Usage:
    pip install -r scripts/requirements.txt
    kubectl -n free5gc port-forward svc/mongo 27017:27017 &
    python3 scripts/provision-subscribers.py --count 5 --clean

The web console must have started at least once before: it creates the
"admin" tenant whose identifier is copied into the documents.
"""
import argparse
import sys

import pymongo

# Collections used by the v4.2.3 web console (backend/WebUI/api_webui.go).
AUTH_COLL = "subscriptionData.authenticationData.authenticationSubscription"
WEB_AUTH_COLL = "subscriptionData.authenticationData.webAuthenticationSubscription"
AM_COLL = "subscriptionData.provisionedData.amData"
SM_COLL = "subscriptionData.provisionedData.smData"
SMF_SEL_COLL = "subscriptionData.provisionedData.smfSelectionSubscriptionData"
AM_POLICY_COLL = "policyData.ues.amData"
SM_POLICY_COLL = "policyData.ues.smData"
IDENTITY_COLL = "subscriptionData.identityData"

ALL_COLLECTIONS = [
    AUTH_COLL, WEB_AUTH_COLL, AM_COLL, SM_COLL,
    SMF_SEL_COLL, AM_POLICY_COLL, SM_POLICY_COLL, IDENTITY_COLL,
]


def snssai_hex(sst, sd):
    """Key used in the smfSelection/smPolicy maps: sst in hex followed by sd."""
    return "%02x%s" % (sst, sd)


def escape_dnn(dnn):
    """Same as EscapeDnn() in the web console: '.' cannot be a MongoDB map key."""
    return dnn.replace(".", "_")


def make_docs(ue_id, args):
    """The eight documents of one UE, in the form written by the web console."""
    snssai = {"sst": args.sst, "sd": args.sd}
    key = snssai_hex(args.sst, args.sd)
    dnn = escape_dnn(args.dnn)

    # The authentication collection stores "encPermanentKey"/"encOpcKey" (in
    # clear, despite the prefix) and "sequenceNumber" as an object.
    auth = {
        "authenticationMethod": "5G_AKA",
        "encPermanentKey": args.key,
        "sequenceNumber": {"sqnScheme": "GENERAL", "sqn": args.sqn},
        "authenticationManagementField": args.amf,
        "encOpcKey": args.opc,
        "ueId": ue_id,
        "tenantId": args.tenant_id,
    }

    # Read only by the web console, to display and edit the subscriber.
    web_auth = {
        "permanentKey": {
            "encryptionKey": 0,
            "encryptionAlgorithm": 0,
            "permanentKeyValue": args.key,
        },
        "sequenceNumber": args.sqn,
        "milenage": {"op": {"opValue": "", "encryptionKey": 0, "encryptionAlgorithm": 0}},
        "opc": {"opcValue": args.opc, "encryptionKey": 0, "encryptionAlgorithm": 0},
        "authenticationManagementField": args.amf,
        "ueId": ue_id,
        "tenantId": args.tenant_id,
        "authenticationMethod": "5G_AKA",
    }

    am_data = {
        "subscribedUeAmbr": {"uplink": args.ue_ambr_ul, "downlink": args.ue_ambr_dl},
        "nssai": {"defaultSingleNssais": [snssai]},
        "ueId": ue_id,
        "servingPlmnId": args.plmn,
        "tenantId": args.tenant_id,
        "gpsis": ["msisdn-"],
    }

    sm_data = {
        "servingPlmnId": args.plmn,
        "singleNssai": snssai,
        "dnnConfigurations": {
            dnn: {
                "pduSessionTypes": {
                    "defaultSessionType": "IPV4",
                    "allowedSessionTypes": ["IPV4"],
                },
                "sscModes": {
                    "defaultSscMode": "SSC_MODE_1",
                    "allowedSscModes": ["SSC_MODE_2", "SSC_MODE_3"],
                },
                "5gQosProfile": {
                    "5qi": 9,
                    "arp": {"preemptCap": "", "preemptVuln": "", "priorityLevel": 8},
                    "priorityLevel": 8,
                },
                "sessionAmbr": {
                    "uplink": args.sess_ambr_ul,
                    "downlink": args.sess_ambr_dl,
                },
            }
        },
        "ueId": ue_id,
    }

    smf_sel = {
        "subscribedSnssaiInfos": {key: {"dnnInfos": [{"dnn": args.dnn}]}},
        "ueId": ue_id,
        "servingPlmnId": args.plmn,
    }

    am_policy = {"subscCats": ["free5gc"], "ueId": ue_id}

    sm_policy = {
        "smPolicySnssaiData": {
            key: {"snssai": snssai, "smPolicyDnnData": {dnn: {"dnn": args.dnn}}}
        },
        "ueId": ue_id,
    }

    identity = {"ueId": ue_id, "gpsi": "msisdn-"}

    return {
        AUTH_COLL: auth,
        WEB_AUTH_COLL: web_auth,
        AM_COLL: am_data,
        SM_COLL: sm_data,
        SMF_SEL_COLL: smf_sel,
        AM_POLICY_COLL: am_policy,
        SM_POLICY_COLL: sm_policy,
        IDENTITY_COLL: identity,
    }


def resolve_tenant_id(db, given):
    """The tenant identifier is generated by the web console on its first boot."""
    if given:
        return given
    doc = db["tenantData"].find_one({"tenantName": "admin"})
    if doc is None:
        sys.exit("tenantData is empty: start the web console once (it creates the "
                 "'admin' tenant at boot) or pass --tenant-id explicitly")
    return doc["tenantId"]


def main():
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--url", default="mongodb://localhost:27017/",
                        help="MongoDB URL (default: localhost, through a port-forward)")
    parser.add_argument("--db", default="free5gc", help="database name (default: free5gc)")
    parser.add_argument("--imsi-base", default="208930000000000",
                        help="first IMSI, without the 'imsi-' prefix (default: "
                             "208930000000000, matching MCC/MNC 208/93 and the UERANSIM supi)")
    parser.add_argument("--count", type=int, default=5,
                        help="number of subscribers (default: 5)")
    parser.add_argument("--clean", action="store_true",
                        help="remove the existing subscribers before inserting")
    parser.add_argument("--plmn", default="20893", help="servingPlmnId (default: 20893)")
    parser.add_argument("--sst", type=int, default=1, help="slice SST (default: 1)")
    parser.add_argument("--sd", default="010203", help="slice SD (default: 010203)")
    parser.add_argument("--dnn", default="internet", help="DNN (default: internet)")
    parser.add_argument("--tenant-id", default=None,
                        help="web console tenantId (default: read from tenantData)")
    # Credentials: defaults equal to ue.yaml in manifests/ran/ueransim.yaml.
    parser.add_argument("--key", default="8baf473f2f8fd09487cccbd7097c6862")
    parser.add_argument("--opc", default="8e27b6af0e692e750f32667a3b14605d")
    parser.add_argument("--amf", default="8000")
    parser.add_argument("--sqn", default="000000000023")
    parser.add_argument("--ue-ambr-ul", default="1 Gbps")
    parser.add_argument("--ue-ambr-dl", default="2 Gbps")
    parser.add_argument("--sess-ambr-ul", default="200 Mbps")
    parser.add_argument("--sess-ambr-dl", default="100 Mbps")
    args = parser.parse_args()

    client = pymongo.MongoClient(args.url)
    db = client[args.db]
    args.tenant_id = resolve_tenant_id(db, args.tenant_id)
    print(f"tenantId: {args.tenant_id}")

    if args.clean:
        for coll in ALL_COLLECTIONS:
            removed = db[coll].delete_many({}).deleted_count
            if removed:
                print(f"removed {removed} documents from {coll}")

    base = int(args.imsi_base)
    width = len(args.imsi_base)
    batches = {coll: [] for coll in ALL_COLLECTIONS}
    for i in range(args.count):
        ue_id = "imsi-" + str(base + i).zfill(width)
        for coll, doc in make_docs(ue_id, args).items():
            batches[coll].append(doc)

    for coll, docs in batches.items():
        db[coll].insert_many(docs)

    last = str(base + args.count - 1).zfill(width)
    print(f"inserted {args.count} subscribers "
          f"(imsi-{args.imsi_base} .. imsi-{last})")
    print(f"slice sst={args.sst} sd={args.sd}, dnn={args.dnn}, plmn={args.plmn}")
    print(f"{args.count * len(ALL_COLLECTIONS)} documents in {len(ALL_COLLECTIONS)} collections")


if __name__ == "__main__":
    main()
