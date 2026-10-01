#!/usr/bin/env python3
"""
Validates the manifests before they are applied to the cluster.

The checks reproduce rules that free5GC v4.2.3 enforces at runtime (taken from
the validation tags in smf/pkg/factory/config.go and go-upf/pkg/factory/
config.go) and the consistency between files. They target the most expensive
class of error: a configuration that is syntactically valid but is rejected by
the network function at boot, or names that do not match across files, which
fail without any PDU session and are hard to trace in the logs.

Usage:
    python3 scripts/validate.py
"""
import glob
import hashlib
import ipaddress
import os
import sys

import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
M = "manifests"
SCENARIO_A = f"{M}/scenario-a-central-upf"
SCENARIO_B = f"{M}/scenario-b-edge-upf"
PLACEHOLDER = "CHANGE-ME"

SMF_VERSIONS = {"1.0.7"}
UPF_VERSIONS = {"1.0.3"}
UDR_VERSIONS = {"1.1.0"}
WEBUI_VERSIONS = {"1.0.3"}

# (ConfigMap name, key, source file) of the ConfigMaps generated from sources.
GENERATED = [
    ("mec-af", "mec_af.py", "af/mec_af.py"),
    ("lbo-probe", "probe.py", "probe/probe.py"),
]


def load_docs(path):
    with open(path) as fh:
        return [d for d in yaml.safe_load_all(fh) if d]


def configmaps(path, loader=yaml.SafeLoader):
    """Returns {'<configmap>/<key>': parsed_config} for the embedded YAML files."""
    out = {}
    for d in load_docs(path):
        if d.get("kind") == "ConfigMap":
            for key, raw in (d.get("data") or {}).items():
                if key.endswith((".yaml", ".yml")):
                    out[f"{d['metadata']['name']}/{key}"] = yaml.load(raw, Loader=loader)
    return out


def configmaps_text(path):
    """Same as configmaps(), with every scalar kept as a string. Needed to
    compare slices: the default loader reads 'sd: 010203' as an octal integer
    and 'sd: 112233' as a decimal one."""
    return configmaps(path, loader=yaml.BaseLoader)


def snssai(sst, sd):
    """Normalised S-NSSAI, e.g. (1, '010203'). Accepts '0x010203' (UERANSIM)
    and '010203' (SMF configuration)."""
    sst, sd = str(sst).strip().lower(), str(sd or "").strip().lower()
    sst = int(sst, 16) if sst.startswith("0x") else int(sst)
    sd = f"{int(sd, 16):06x}" if sd.startswith("0x") else sd.zfill(6)
    return sst, sd


def check_smf(path, conf, errors):
    c = conf["configuration"]
    version = str(conf["info"]["version"])
    if version not in SMF_VERSIONS:
        errors.append(f"{path}: smf info.version={version}, expected {SMF_VERSIONS}")

    for name, node in c["userplaneInformation"]["upNodes"].items():
        if node["type"] not in ("AN", "UPF"):
            errors.append(f"{path}: upNode {name} type='{node['type']}' (use AN or UPF)")
        for iface in node.get("interfaces", []):
            if iface["interfaceType"] not in ("N3", "N9"):
                errors.append(f"{path}: {name} has an invalid interfaceType")
            for endpoint in iface["endpoints"]:
                if endpoint == "0.0.0.0":
                    errors.append(
                        f"{path}: {name} has endpoint 0.0.0.0; this address is sent to "
                        "the peer in the F-TEID and must be routable")

    pools = []
    for name, node in c["userplaneInformation"]["upNodes"].items():
        for s in node.get("sNssaiUpfInfos", []):
            for d in s.get("dnnUpfInfoList", []):
                for p in d.get("pools", []):
                    pools.append((name, ipaddress.ip_network(p["cidr"], strict=False)))
    for i, (na, a) in enumerate(pools):
        for nb, b in pools[i + 1:]:
            if a.overlaps(b):
                errors.append(
                    f"{path}: UE pools {a} ({na}) and {b} ({nb}) overlap; the SMF checks "
                    "pools globally, not per slice, and aborts at boot")

    metrics = c.get("metrics")
    if metrics:
        if "scheme" not in metrics:
            errors.append(f"{path}: metrics requires 'scheme'")
        if (metrics.get("port") == c["sbi"]["port"]
                and metrics.get("bindingIPv4") == c["sbi"]["bindingIPv4"]):
            errors.append(f"{path}: metrics and sbi share the same address and port")


def check_upf(path, conf, errors, warnings):
    version = str(conf["version"])
    if version not in UPF_VERSIONS:
        errors.append(f"{path}: upf version={version}, expected {UPF_VERSIONS}")
    if conf["pfcp"]["addr"] == "0.0.0.0":
        errors.append(f"{path}: pfcp.addr must not be 0.0.0.0")
    if conf["gtpu"]["forwarder"] != "gtp5g":
        errors.append(f"{path}: gtpu.forwarder must be gtp5g")
    for iface in conf["gtpu"]["ifList"]:
        if iface["type"] not in ("N3", "N9"):
            errors.append(f"{path}: gtpu.ifList has an invalid type")
    if len(conf["gtpu"]["ifList"]) > 1:
        warnings.append(
            f"{path}: gtpu.ifList has {len(conf['gtpu']['ifList'])} entries, but go-upf "
            "only uses the first one; prefer a single 0.0.0.0 entry")


def check_node_ids(smf_path, upf_paths, errors):
    """nodeID declared in the SMF against the nodeID announced by each UPF."""
    smf = next(v for k, v in configmaps(smf_path).items() if "smfConfig" in k)["configuration"]
    declared = {n["nodeID"] for n in smf["userplaneInformation"]["upNodes"].values()
                if n["type"] == "UPF"}
    for path in upf_paths:
        for key, conf in configmaps(path).items():
            if "upfConfig" in key and conf["pfcp"]["nodeID"] not in declared:
                errors.append(
                    f"{path}: nodeID '{conf['pfcp']['nodeID']}' is not declared in "
                    f"{smf_path} {sorted(declared)}; the PFCP association would never "
                    "be established")


def check_scenario_b(errors):
    smf_path = f"{SCENARIO_B}/smf.yaml"
    maps = configmaps(smf_path)
    smf = next(v for k, v in maps.items() if "smfConfig" in k)["configuration"]
    routing = next(v for k, v in maps.items() if "ueRouting" in k)

    if not smf.get("ulcl"):
        errors.append("scenario B: 'ulcl: true' is missing; the UE routing file would be "
                      "ignored entirely")

    nodes = set(smf["userplaneInformation"]["upNodes"])
    for group, info in routing["ueRoutingInfo"].items():
        for link in info["topology"]:
            for side in (link["A"], link["B"]):
                if side not in nodes:
                    errors.append(f"ueRouting[{group}]: node '{side}' does not exist in "
                                  f"upNodes {sorted(nodes)}")
        for spec in info.get("specificPath", []):
            try:
                ipaddress.ip_network(spec["dest"], strict=False)
            except ValueError as exc:
                errors.append(f"ueRouting[{group}]: invalid dest '{spec['dest']}' ({exc})")
            for hop in spec["path"]:
                if hop not in nodes:
                    errors.append(f"ueRouting[{group}]: path '{hop}' does not exist in upNodes")

    # Dynamic breakout: every name in SERVICES must exist as a Service (the AF
    # resolves it through DNS), none of them may pin a ClusterIP, and some UPF
    # must declare the announced DNAI in every announced slice.
    services = {d["metadata"]["name"]: d
                for d in load_docs(f"{M}/apps/apps.yaml") if d.get("kind") == "Service"}
    af = next(d for d in load_docs(f"{M}/apps/mec-af.yaml")
              if d.get("kind") == "Deployment" and d["metadata"]["name"] == "mec-af")
    af_env = {e["name"]: e.get("value")
              for e in af["spec"]["template"]["spec"]["containers"][0]["env"]}

    announced = [s.strip() for s in (af_env.get("SERVICES") or "").split(",") if s.strip()]
    if not announced:
        errors.append("mec-af has an empty SERVICES: no service would be announced to the NEF")
    for name in announced:
        short = name.split(".")[0]
        if short not in services:
            errors.append(f"mec-af announces '{name}' but there is no Service '{short}' in "
                          f"apps.yaml ({sorted(services)}); the name would not resolve")
        elif "clusterIP" in services[short]["spec"]:
            errors.append(f"{short}: fixed clusterIP in the manifest; the dynamic breakout "
                          "does not need it and it collides if the address is already taken")
    if "remote-app" in [n.split(".")[0] for n in announced]:
        errors.append("mec-af announces remote-app; it is the reference path and must keep "
                      "reaching the PSA-UPF")

    smf_txt = next(v for k, v in configmaps_text(smf_path).items()
                   if "smfConfig" in k)["configuration"]
    dnai = af_env.get("DNAI")
    for item in (af_env.get("SNSSAIS") or "").split(","):
        sst, _, sd = item.strip().partition(":")
        if not sst:
            continue
        target = snssai(sst, sd)
        with_dnai = [
            name for name, n in smf_txt["userplaneInformation"]["upNodes"].items()
            if n["type"] == "UPF"
            for s in n.get("sNssaiUpfInfos", [])
            if snssai(s["sNssai"]["sst"], s["sNssai"].get("sd")) == target
            for d in s.get("dnnUpfInfoList", [])
            if dnai in d.get("dnaiList", [])
        ]
        if not with_dnai:
            errors.append(
                f"mec-af announces slice {target[0]}:{target[1]}, but no UPF in {smf_path} "
                f"declares the DNAI '{dnai}' in that slice; the breakout rule would have "
                "no path")

    check_node_ids(smf_path, (f"{SCENARIO_B}/i-upf.yaml", f"{SCENARIO_B}/psa-upf.yaml"), errors)


def check_ran(errors):
    """The first UE of UERANSIM must be a member of the ULCL group."""
    ue = next((v for k, v in configmaps_text(f"{M}/ran/ueransim.yaml").items()
               if k.endswith("/ue.yaml")), None)
    routing = next(v for k, v in configmaps(f"{SCENARIO_B}/smf.yaml").items()
                   if "ueRouting" in k)
    members = {m for info in routing["ueRoutingInfo"].values()
               for m in (info.get("members") or [])}
    if ue and ue["supi"] not in members:
        errors.append(f"UERANSIM supi {ue['supi']} is not listed in ueRoutingInfo.members; "
                      "the UE would not receive the ULCL treatment")


def check_generated(errors):
    """The generated ConfigMaps must match the current sources."""
    path = f"{M}/apps/code-configmaps.yaml"
    try:
        docs = {d["metadata"]["name"]: d for d in load_docs(path)}
    except FileNotFoundError:
        errors.append(f"{path} is missing; run scripts/gen-configmaps.py")
        return
    for name, key, source in GENERATED:
        with open(source, "rb") as fh:
            digest = hashlib.sha256(fh.read()).hexdigest()
        doc = docs.get(name)
        recorded = (doc or {}).get("metadata", {}).get("annotations", {}).get("source-sha256")
        if doc is None or key not in (doc.get("data") or {}):
            errors.append(f"{path}: ConfigMap '{name}' with key '{key}' is missing; "
                          "run scripts/gen-configmaps.py")
        elif recorded != digest:
            errors.append(f"{path}: ConfigMap '{name}' is older than {source}; "
                          "run scripts/gen-configmaps.py")


def check_placeholders(warnings):
    pending = []
    for path in sorted(glob.glob(f"{M}/**/*.yaml", recursive=True)):
        with open(path) as fh:
            if any(PLACEHOLDER in line and not line.strip().startswith("#") for line in fh):
                pending.append(path)
    if pending:
        warnings.append(
            f"node placeholders ({PLACEHOLDER}-*) remain in {len(pending)} manifest(s); "
            "replace them before deploying (docs/02, section 2.4)")


def main():
    os.chdir(ROOT)
    errors, warnings = [], []

    for path in sorted(glob.glob(f"{M}/**/*.yaml", recursive=True)):
        try:
            for key, conf in configmaps(path).items():
                if "smfConfig" in key:
                    check_smf(path, conf, errors)
                elif "upfConfig" in key:
                    check_upf(path, conf, errors, warnings)
                elif "udrConfig" in key and str(conf["info"]["version"]) not in UDR_VERSIONS:
                    errors.append(f"{path}: udr info.version must be {UDR_VERSIONS} in v4.2.3")
                elif "webuiConfig" in key and str(conf["info"]["version"]) not in WEBUI_VERSIONS:
                    errors.append(f"{path}: webui info.version must be {WEBUI_VERSIONS} in v4.2.3")
        except (yaml.YAMLError, KeyError, TypeError) as exc:
            errors.append(f"{path}: could not be interpreted ({exc})")

    check_scenario_b(errors)
    check_node_ids(f"{SCENARIO_A}/smf.yaml", (f"{SCENARIO_A}/upf.yaml",), errors)
    check_ran(errors)
    check_generated(errors)
    check_placeholders(warnings)

    for w in warnings:
        print(f"WARNING: {w}")
    for e in errors:
        print(f"ERROR  : {e}")

    if errors:
        print(f"\n{len(errors)} problem(s) found.")
        return 1
    print("\nManifests are consistent.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
