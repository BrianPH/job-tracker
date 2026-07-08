#!/usr/bin/env python3
"""Start the local web UI + daily scheduler.

Usage:
    python run_web.py            # local only: http://127.0.0.1:8000
    python run_web.py --lan      # also reachable from phones/laptops on your network
    python run_web.py --port 8080

--lan binds to 0.0.0.0 and prints the address to open on other devices.
Windows will ask once to allow Python through the firewall — allow it for
private networks. Note: the app has no login, so only use --lan on networks
you trust (home wifi).
"""
from __future__ import annotations

import argparse
import socket

import uvicorn


def lan_ip() -> str:
    """Best-effort LAN IP (no traffic is actually sent)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except OSError:
        return socket.gethostbyname(socket.gethostname())
    finally:
        s.close()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--lan", action="store_true",
                    help="listen on all interfaces so other devices on your network can connect")
    ap.add_argument("--host", default=None, help="explicit bind address (overrides --lan)")
    ap.add_argument("--port", type=int, default=8000)
    args = ap.parse_args()

    host = args.host or ("0.0.0.0" if args.lan else "127.0.0.1")
    print(f"Local:   http://127.0.0.1:{args.port}")
    if host == "0.0.0.0":
        print(f"Network: http://{lan_ip()}:{args.port}   (open this on your phone, same wifi)")
        print("         Geen login — gebruik --lan alleen op vertrouwde netwerken.")
    uvicorn.run("jobtracker.webapp:app", host=host, port=args.port)


if __name__ == "__main__":
    main()
