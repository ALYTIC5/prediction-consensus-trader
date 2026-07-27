"""Launch the operations console.

Host is hardcoded to 127.0.0.1, never 0.0.0.0 - the dashboard has no
authentication (see docs/PHASE8_DESIGN.md), and that's only safe because
it's unreachable from anywhere but this machine. Remote access waits for
Phase 9 and real auth; binding wider than localhost before then would
expose the tuning panel's mutating endpoints to the network.
"""

import uvicorn

if __name__ == "__main__":
    uvicorn.run("app.dashboard.main:app", host="127.0.0.1", port=8000)
