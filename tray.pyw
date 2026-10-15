"""Run PatchGoblin in the background with a system tray icon.

Click the icon (or choose Open PatchGoblin) to open the app in your browser; Restart stops
the server and starts it again (after asking, if AI jobs are running); Quit stops the server. Started as .pyw so Windows runs it without a console window; the server log
goes to patchgoblin.log in the data directory. PATCHGOBLIN_PORT / PATCHGOBLIN_DATA apply
as for run.py. If PatchGoblin is already running on the port, this just opens it.
"""
import logging
import logging.handlers
import os
import socket
import subprocess
import sys
import threading
import time
import webbrowser

PORT = int(os.environ.get("PATCHGOBLIN_PORT", "5050"))
URL = f"http://127.0.0.1:{PORT}"
DATA_DIR = os.environ.get("PATCHGOBLIN_DATA") or os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")


def setup_logging() -> None:
    os.makedirs(DATA_DIR, exist_ok=True)
    handlers = [logging.handlers.RotatingFileHandler(
        os.path.join(DATA_DIR, "patchgoblin.log"), maxBytes=1_000_000, backupCount=3, encoding="utf-8")]
    if sys.stderr:  # None under pythonw
        handlers.append(logging.StreamHandler())
    logging.basicConfig(level=logging.INFO, handlers=handlers,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")


def already_running() -> bool:
    with socket.socket() as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", PORT)) == 0


def wait_for_port_free(timeout: float = 10.0) -> None:
    """After a restart, give the old server time to release the port."""
    deadline = time.monotonic() + timeout
    while already_running() and time.monotonic() < deadline:
        time.sleep(0.25)


def relaunch() -> None:
    """Start a new detached copy of this script with the same interpreter and environment."""
    script = os.path.abspath(__file__)
    kwargs = {}
    if os.name == "nt":
        kwargs["creationflags"] = (subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
                                   | subprocess.CREATE_NO_WINDOW)
    else:
        kwargs["start_new_session"] = True
    cmd = [sys.executable, script, "--restarted"]
    common = dict(stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                  close_fds=True, cwd=os.path.dirname(script))
    try:
        subprocess.Popen(cmd, **common, **kwargs)
    except OSError:
        if os.name != "nt":
            raise
        subprocess.Popen(cmd, **common, creationflags=subprocess.CREATE_NEW_PROCESS_GROUP
                         | subprocess.CREATE_NO_WINDOW)


def fail(message: str) -> None:
    logging.error(message)
    if os.name == "nt":
        import ctypes
        ctypes.windll.user32.MessageBoxW(None, message, "PatchGoblin", 0x10)
    sys.exit(1)


def confirm(message: str) -> bool:
    """Yes/No warning dialog (Windows only; elsewhere assume yes)."""
    if os.name != "nt":
        return True
    import ctypes
    return ctypes.windll.user32.MessageBoxW(None, message, "PatchGoblin", 0x4 | 0x30 | 0x40000) == 6


def make_icon_image():
    """The favicon goblin crown, drawn at 64x64 (2x the SVG's 32-unit grid)."""
    from PIL import Image, ImageDraw
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    green, yellow = "#2f7d4f", "#f4e27a"
    d.polygon([(8, 18), (20, 24), (32, 10), (44, 24), (56, 18), (50, 48), (14, 48)], fill=green)
    for cx in (25, 39):
        d.ellipse((cx - 4, 30, cx + 4, 38), fill=yellow)
    d.rounded_rectangle((14, 48, 50, 54), radius=2, fill=green)
    return img


def main() -> None:
    setup_logging()
    if "--restarted" in sys.argv[1:]:
        wait_for_port_free()
    if already_running():
        webbrowser.open(URL)
        return

    import pystray
    from waitress import create_server

    from patchgoblin import create_app

    try:
        app = create_app()
        server = create_server(app, host="127.0.0.1", port=PORT, threads=8)
    except OSError as e:
        fail(f"PatchGoblin could not listen on port {PORT}: {e}")
    threading.Thread(target=server.run, name="pg-server", daemon=True).start()
    logging.info("PatchGoblin running at %s (tray)", URL)

    def open_app(icon, item):
        webbrowser.open(URL)

    def quit_app(icon, item):
        icon.stop()

    def restart_app(icon, item):
        nonlocal restart
        engine = app.config["ENGINE"]
        busy = bool(engine.jobs) or any(c.job is not None for c in list(engine.chats.values()))
        if busy and not confirm("Restarting will interrupt running AI jobs. Restart anyway?"):
            return
        restart = True
        icon.stop()

    restart = False
    icon = pystray.Icon("PatchGoblin", make_icon_image(), f"PatchGoblin ({URL})", menu=pystray.Menu(
        pystray.MenuItem("Open PatchGoblin", open_app, default=True),
        pystray.MenuItem("Restart", restart_app),
        pystray.MenuItem("Quit", quit_app),
    ))
    icon.run()  # blocks on the main thread until Quit or Restart
    logging.info("PatchGoblin restarting from the tray" if restart else "PatchGoblin stopped from the tray")
    server.close()
    app.config["PLUGINS"].stop_all()  # e.g. SSH tools stops its tunnels
    if restart:
        try:
            relaunch()
        except OSError as e:
            logging.exception("PatchGoblin could not restart")
            fail(f"PatchGoblin could not restart: {e}")


if __name__ == "__main__":
    main()
