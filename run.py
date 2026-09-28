"""Start PatchGoblin on http://127.0.0.1:5050 (PATCHGOBLIN_PORT to change)."""
import logging
import os

from patchgoblin import create_app

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

if __name__ == "__main__":
    port = int(os.environ.get("PATCHGOBLIN_PORT", "5050"))
    app = create_app()
    print(f"PatchGoblin running at http://127.0.0.1:{port}")
    try:
        from waitress import serve
        serve(app, host="127.0.0.1", port=port, threads=8)
    except ImportError:
        app.run(host="127.0.0.1", port=port, threaded=True, use_reloader=False)
