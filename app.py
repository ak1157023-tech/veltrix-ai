"""VELTRIX AI - Phase 1 (simple version). Run:  python app.py"""
import os
import shutil
import subprocess
import threading
import webbrowser
from pathlib import Path

import uvicorn
from fastapi import FastAPI
from fastapi.responses import FileResponse


BASE = Path(__file__).parent
app = FastAPI(title="VELTRIX AI")

# AI Cartoon Studio: if it cannot load (e.g. a missing package) the site still starts and shows the reason.
try:
    from cartoon_routes import router as cartoon_router

    app.include_router(cartoon_router)
except Exception as exc:  # noqa: BLE001
    CARTOON_ERROR = f"{type(exc).__name__}: {exc}"

    @app.get("/api/cartoon/status")
    def cartoon_status_failed():
        return {"ready": False, "missing": ["Cartoon backend failed to load -> " + CARTOON_ERROR]}

ENCODERS = {
    "h264_nvenc": "NVIDIA NVENC (H.264)",
    "hevc_nvenc": "NVIDIA NVENC (H.265)",
    "h264_qsv": "Intel Quick Sync (H.264)",
    "hevc_qsv": "Intel Quick Sync (H.265)",
    "h264_amf": "AMD AMF (H.264)",
    "hevc_amf": "AMD AMF (H.265)",
    "libx264": "CPU x264 (H.264)",
    "libx265": "CPU x265 (H.265)",
    "libvpx-vp9": "CPU VP9 (WebM)",
}


def run(args):
    return subprocess.run(args, capture_output=True, text=True, timeout=15).stdout


@app.get("/")
def home():
    return FileResponse(BASE / "index.html", headers={"Cache-Control": "no-store"})  # always load the newest page


@app.get("/api/system")
def system():
    path = shutil.which("ffmpeg")
    if not path:
        return {"ffmpeg": False, "version": None, "encoders": []}
    version = run([path, "-version"]).splitlines()[0]
    out = run([path, "-hide_banner", "-encoders"])
    found = [label for key, label in ENCODERS.items() if key in out]
    return {"ffmpeg": True, "version": version, "encoders": found}


if __name__ == "__main__":
    # On Render the PORT variable is set; locally we use 8000 and open the browser.
    port = int(os.environ.get("PORT", 8000))
    on_server = "PORT" in os.environ
    if not on_server:
        threading.Timer(1.5, lambda: webbrowser.open(f"http://localhost:{port}")).start()
    uvicorn.run(app, host="0.0.0.0" if on_server else "127.0.0.1", port=port)
