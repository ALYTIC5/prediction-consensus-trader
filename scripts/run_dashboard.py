"""Launch the operations console.

Host and port both come from whether PORT is set (Railway injects it):
unset -> 127.0.0.1:8000, the original localhost-only local-dev default,
since the dashboard has no authentication there and that's only safe
because it's unreachable from anywhere but this machine. PORT set -> the
dashboard is presumed to be running in a hosted container instead, so it
binds 0.0.0.0:$PORT to be reachable through Railway's proxy - safe there
specifically because app/dashboard/main.py requires DASHBOARD_USER/
DASHBOARD_PASSWORD to be set whenever ENVIRONMENT=production.
"""

import uvicorn

from app.config.settings import get_settings

if __name__ == "__main__":
    settings = get_settings()
    if settings.port is not None:
        uvicorn.run("app.dashboard.main:app", host="0.0.0.0", port=settings.port)
    else:
        uvicorn.run("app.dashboard.main:app", host="127.0.0.1", port=8000)
