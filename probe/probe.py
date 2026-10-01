#!/usr/bin/env python3
"""
UDP echo probe used to exercise the user plane of the tutorial.

It is deliberately minimal: its purpose is to put a known number of packets
on a chosen path, so that the packet counters of the UPFs can be compared.
The round-trip times it reports are indicative only; they are taken in user
space and are not a substitute for a calibrated measurement tool.

Server (edge and remote applications):

    probe.py server --port 9000

Client (inside the UE pod, bound to the address of the PDU session):

    probe.py client --server 10.96.12.34:9000 --bind 60.60.0.1 --count 500

Standard library only; runs on a stock python:3.12-slim image.
"""
import argparse
import json
import select
import socket
import struct
import sys
import time

HEADER = struct.Struct("!4sIQ")     # magic, sequence number, send time (ns)
MAGIC = b"LBOP"


def run_server(args):
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((args.bind, args.port))
    print(f"probe server listening on udp {args.bind}:{args.port}", flush=True)
    packets = 0
    while True:
        data, peer = sock.recvfrom(65535)
        sock.sendto(data, peer)
        packets += 1
        if args.verbose and packets % 1000 == 0:
            print(f"echoed {packets} packets (last peer {peer[0]})", flush=True)


def percentile(sorted_values, fraction):
    if not sorted_values:
        return None
    index = min(len(sorted_values) - 1, int(round(fraction * (len(sorted_values) - 1))))
    return sorted_values[index]


def drain(sock, rtts, seen, deadline):
    """Reads echoes until the deadline, recording one RTT per sequence number."""
    while True:
        wait = deadline - time.monotonic()
        if wait <= 0:
            return
        ready, _, _ = select.select([sock], [], [], wait)
        if not ready:
            return
        try:
            data = sock.recv(65535)
        except OSError:
            return
        now = time.monotonic_ns()
        if len(data) < HEADER.size:
            continue
        magic, seq, sent_ns = HEADER.unpack_from(data)
        if magic != MAGIC or seq in seen:
            continue
        seen.add(seq)
        rtts.append((now - sent_ns) / 1e6)


def run_client(args):
    host, _, port = args.server.rpartition(":")
    if not host or not port.isdigit():
        sys.exit("--server must be HOST:PORT")
    try:
        target = (socket.gethostbyname(host), int(port))
    except socket.gaierror as exc:
        sys.exit(f"cannot resolve {host}: {exc}")

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind((args.bind, 0))
    sock.setblocking(False)

    padding = b"\x00" * max(0, args.size - HEADER.size)
    interval = args.interval_ms / 1000.0
    rtts, seen, sent = [], set(), 0
    start = time.monotonic()

    for seq in range(args.count):
        packet = HEADER.pack(MAGIC, seq, time.monotonic_ns()) + padding
        try:
            sock.sendto(packet, target)
            sent += 1
        except OSError as exc:
            print(f"send failed at seq {seq}: {exc}", file=sys.stderr)
        drain(sock, rtts, seen, start + (seq + 1) * interval)

    drain(sock, rtts, seen, time.monotonic() + args.timeout)

    rtts.sort()
    received = len(rtts)
    summary = {
        "label": args.label,
        "server": f"{target[0]}:{target[1]}",
        "bind": args.bind,
        "sent": sent,
        "received": received,
        "loss_pct": round(100.0 * (sent - received) / sent, 3) if sent else None,
        "rtt_ms": {
            "min": round(rtts[0], 3) if rtts else None,
            "p50": round(percentile(rtts, 0.50), 3) if rtts else None,
            "p95": round(percentile(rtts, 0.95), 3) if rtts else None,
            "max": round(rtts[-1], 3) if rtts else None,
        },
    }
    print(json.dumps(summary, indent=2))
    return 0 if received else 1


def main():
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="mode", required=True)

    server = sub.add_parser("server", help="echo every datagram back to its sender")
    server.add_argument("--bind", default="0.0.0.0", help="listen address (default: 0.0.0.0)")
    server.add_argument("--port", type=int, default=9000, help="UDP port (default: 9000)")
    server.add_argument("--verbose", action="store_true", help="log every 1000 packets")

    client = sub.add_parser("client", help="send datagrams and report loss and round-trip time")
    client.add_argument("--server", required=True, help="HOST:PORT of the probe server")
    client.add_argument("--bind", required=True,
                        help="source address; must be the address of the PDU session")
    client.add_argument("--count", type=int, default=500, help="datagrams to send (default: 500)")
    client.add_argument("--interval-ms", type=float, default=10.0,
                        help="interval between datagrams, in ms (default: 10)")
    client.add_argument("--size", type=int, default=200,
                        help="UDP payload size, in bytes (default: 200)")
    client.add_argument("--timeout", type=float, default=1.0,
                        help="seconds to wait for late echoes after the last send (default: 1)")
    client.add_argument("--label", default="probe", help="tag copied to the summary")

    args = parser.parse_args()
    if args.mode == "server":
        try:
            run_server(args)
        except KeyboardInterrupt:
            return 0
    return run_client(args)


if __name__ == "__main__":
    sys.exit(main())
