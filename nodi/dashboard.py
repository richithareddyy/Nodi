"""Public static dashboard; all data and mutations use authenticated API routes."""

from importlib.resources import files

from fastapi import HTTPException
from fastapi.responses import Response

HEADERS = {
    "Cache-Control": "no-store",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "X-Frame-Options": "DENY",
    "Content-Security-Policy": (
        "default-src 'none'; script-src 'self'; style-src 'self'; "
        "connect-src 'self'; img-src 'self'; base-uri 'none'; "
        "form-action 'none'; frame-ancestors 'none'"
    ),
}


def install_dashboard(app):
    def asset(name, media_type):
        content = files("nodi").joinpath("web", name).read_bytes()
        if name == "index.html" and getattr(app.state, "hosted_demo", False):
            content = content.replace(b'data-hosted="false"', b'data-hosted="true"')
            content = content.replace(b"LOCAL WORKSPACE", b"HOSTED DEMO")
            content = content.replace(
                b"Connect your local workspace", b"Connect your hosted workspace"
            )
            content = content.replace(
                b"Choose the private configuration created by Nodi to get\n              started.",
                b"Enter your two hosted access tokens below to connect securely.",
            )
        return Response(content, media_type=media_type, headers=HEADERS)

    @app.get("/", include_in_schema=False)
    def dashboard():
        return asset("index.html", "text/html")

    @app.get("/favicon.ico", include_in_schema=False)
    def favicon():
        return asset("favicon.ico", "image/x-icon")

    @app.get("/dashboard/{name}", include_in_schema=False)
    def dashboard_asset(name: str):
        allowed = {
            "app.js": "text/javascript",
            "style.css": "text/css",
            "favicon.svg": "image/svg+xml",
            "presentation.js": "text/javascript",
            "social-preview.png": "image/png",
        }
        if name not in allowed:
            raise HTTPException(404, "asset not found")
        return asset(name, allowed[name])
