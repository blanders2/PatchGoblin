"""Install PatchGoblin on Linux: tray launcher, icon, optional autostart and systemd user service.

Everything goes under the user's XDG directories (no root needed) and points at this checkout:

    python3 install_linux.py [--service/--no-service] [--autostart/--no-autostart] [--yes]
    python3 install_linux.py --uninstall

--uninstall removes what the installer wrote; data/ and .venv are never touched.
"""
import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent
UNIT = "patchgoblin.service"
APT_HINT = "sudo apt install python3-gi gir1.2-ayatanaappindicator3-0.1"
GI_CHECK = ("import gi\n"
            "for n in ('AyatanaAppIndicator3', 'AppIndicator3'):\n"
            "    try:\n"
            "        gi.require_version(n, '0.1'); break\n"
            "    except ValueError:\n"
            "        pass\n"
            "else:\n"
            "    raise SystemExit(1)\n")


def paths(home) -> dict[str, Path]:
    home = Path(home)
    icons = home / ".local/share/icons/hicolor"
    return {
        "icon_svg": icons / "scalable/apps/patchgoblin.svg",
        "icon_png": icons / "64x64/apps/patchgoblin.png",
        "icon_root": icons,
        "desktop": home / ".local/share/applications/patchgoblin.desktop",
        "autostart": home / ".config/autostart/patchgoblin.desktop",
        "unit": home / ".config/systemd/user" / UNIT,
    }


def _exec_quote(arg: str) -> str:
    """Quote one Exec= argument per the Desktop Entry spec (also escape % as %%)."""
    arg = arg.replace("\\", "\\\\\\\\")  # backslash: escaped for the quoting layer and the string layer
    for ch in ('"', "`", "$"):
        arg = arg.replace(ch, "\\\\" + ch)
    return '"' + arg.replace("%", "%%") + '"'


def desktop_entry(repo, service: bool) -> str:
    repo = Path(repo)
    exec_line = f'{_exec_quote(str(repo / ".venv/bin/python"))} {_exec_quote(str(repo / "tray.pyw"))}'
    if service:
        exec_line += " --service"
    return ("[Desktop Entry]\n"
            "Type=Application\n"
            "Name=PatchGoblin\n"
            "Comment=Plan and run AI coding tasks\n"
            "Icon=patchgoblin\n"
            f"Exec={exec_line}\n"
            f"Path={repo}\n"
            "Categories=Development;\n"
            "StartupNotify=false\n"
            "Terminal=false\n")


def service_unit(repo, port=None, data=None, path=None) -> str:
    repo = Path(repo)
    env = []
    if port:
        env.append(f"PATCHGOBLIN_PORT={port}")
    if data:
        env.append(f"PATCHGOBLIN_DATA={data}")
    if path:
        env.append(f"PATH={path}")
    lines = ["[Unit]", "Description=PatchGoblin server", "", "[Service]",
             f"ExecStart={repo / '.venv/bin/python'} {repo / 'run.py'}",
             f"WorkingDirectory={repo}"]
    lines += [f'Environment="{e}"' for e in env]
    lines += ["Restart=on-failure", "RestartSec=5", "", "[Install]", "WantedBy=default.target", ""]
    return "\n".join(lines)


def run(cmd, **kw) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, **kw)


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def system_python_has_gi() -> bool:
    py = "/usr/bin/python3"
    if not os.path.exists(py):
        return False
    return run([py, "-c", GI_CHECK], capture_output=True).returncode == 0


def setup_venv() -> None:
    if system_python_has_gi():
        run(["uv", "venv", "--system-site-packages", "--python", "/usr/bin/python3", "--allow-existing"],
            cwd=REPO, check=True)
    else:
        print("Note: system PyGObject + AppIndicator not found. GNOME/COSMIC panels won't show the\n"
              f"tray icon without them. On Debian/Ubuntu/Pop!_OS: {APT_HINT}\n"
              "then re-run this installer.")
    run(["uv", "sync"], cwd=REPO, check=True)


def install_icons(home) -> list[Path]:
    p = paths(home)
    p["icon_svg"].parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(REPO / "patchgoblin/static/favicon.svg", p["icon_svg"])
    p["icon_png"].parent.mkdir(parents=True, exist_ok=True)
    code = ("import importlib.machinery, importlib.util, sys\n"
            "loader = importlib.machinery.SourceFileLoader('tray', sys.argv[1])\n"
            "spec = importlib.util.spec_from_loader('tray', loader)\n"
            "mod = importlib.util.module_from_spec(spec); loader.exec_module(mod)\n"
            "mod.make_icon_image().save(sys.argv[2])\n")
    run([str(REPO / ".venv/bin/python"), "-c", code, str(REPO / "tray.pyw"), str(p["icon_png"])], check=True)
    if shutil.which("gtk-update-icon-cache"):
        run(["gtk-update-icon-cache", "-f", "-t", str(p["icon_root"])], capture_output=True)
    return [p["icon_svg"], p["icon_png"]]


def refresh_desktop_db(home) -> None:
    if shutil.which("update-desktop-database"):
        run(["update-desktop-database", str(paths(home)["desktop"].parent)], capture_output=True)


def install(home, service: bool, autostart: bool) -> list[Path]:
    p = paths(home)
    written = install_icons(home)
    entry = desktop_entry(REPO, service)
    write(p["desktop"], entry)
    written.append(p["desktop"])
    refresh_desktop_db(home)
    if autostart:
        write(p["autostart"], entry + "X-GNOME-Autostart-enabled=true\n")
        written.append(p["autostart"])
    elif p["autostart"].exists():
        p["autostart"].unlink()
    if service:
        write(p["unit"], service_unit(REPO, os.environ.get("PATCHGOBLIN_PORT"),
                                      os.environ.get("PATCHGOBLIN_DATA"), os.environ.get("PATH")))
        written.append(p["unit"])
        run(["systemctl", "--user", "daemon-reload"], check=True)
        run(["systemctl", "--user", "enable", "--now", UNIT], check=True)
    return written


def uninstall(home) -> list[Path]:
    p = paths(home)
    if p["unit"].exists() and shutil.which("systemctl"):
        run(["systemctl", "--user", "disable", "--now", UNIT], capture_output=True)
    removed = []
    for key in ("unit", "desktop", "autostart", "icon_svg", "icon_png"):
        if p[key].exists():
            p[key].unlink()
            removed.append(p[key])
    if p["unit"] in removed and shutil.which("systemctl"):
        run(["systemctl", "--user", "daemon-reload"], capture_output=True)
    refresh_desktop_db(home)
    if shutil.which("gtk-update-icon-cache") and p["icon_root"].exists():
        run(["gtk-update-icon-cache", "-f", "-t", str(p["icon_root"])], capture_output=True)
    return removed


def ask(question: str, default: bool, assume_yes: bool) -> bool:
    if assume_yes or not sys.stdin.isatty():
        return default
    answer = input(f"{question} [{'Y/n' if default else 'y/N'}] ").strip().lower()
    return default if not answer else answer.startswith("y")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Install PatchGoblin's tray launcher on Linux.")
    ap.add_argument("--service", action=argparse.BooleanOptionalAction, default=None,
                    help="run the server as a systemd user service (the tray attaches to it)")
    ap.add_argument("--autostart", action=argparse.BooleanOptionalAction, default=None,
                    help="start the tray at login")
    ap.add_argument("--uninstall", action="store_true", help="remove everything this installer wrote")
    ap.add_argument("--yes", action="store_true", help="don't ask; use defaults for unset options")
    args = ap.parse_args(argv)
    if not sys.platform.startswith("linux"):
        print("install_linux.py only supports Linux.", file=sys.stderr)
        return 1
    home = Path.home()
    if args.uninstall:
        removed = uninstall(home)
        print("Removed:" if removed else "Nothing to remove.")
        for r in removed:
            print(f"  {r}")
        print("data/ and .venv were left untouched.")
        return 0
    missing = [t for t in ("uv", "git") if not shutil.which(t)]
    if missing:
        print(f"Missing required tool(s) on PATH: {', '.join(missing)}", file=sys.stderr)
        return 1
    autostart = args.autostart if args.autostart is not None else ask("Start the tray at login?", True, args.yes)
    service = args.service if args.service is not None else ask(
        "Run the server as a systemd user service?", False, args.yes)
    setup_venv()
    written = install(home, service, autostart)
    print("Installed:")
    for w in written:
        print(f"  {w}")
    if service:
        print("Service logs: journalctl --user -u patchgoblin\n"
              "To start the service at boot without logging in: loginctl enable-linger $USER")
    return 0


if __name__ == "__main__":
    sys.exit(main())
