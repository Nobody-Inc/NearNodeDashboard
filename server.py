#!/usr/bin/env python3
"""Near Node Dashboard: collects server and NEAR node metrics and serves a local dashboard.

The code lives in the dashboard/ package: dashboard/core (settings, storage, HTTP API, alerts) and
one package per module under dashboard/modules ("system": host statistics and health checks;
"near": the NEAR node, validator, peers and logs). Choose modules with "modules" in config.json.

Run:  python3 server.py [--host 0.0.0.0] [--port 8787] [--config config.json]
Then open http://<this-machine's-ip>:8787 (or http://127.0.0.1:8787 locally)
"""

from dashboard.core.app import main

if __name__ == "__main__":
    main(__doc__)
