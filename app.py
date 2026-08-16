"""Crafty Mod Manager - entry point.

    python app.py            normal launch
    python app.py --debug    verbose logging to the console and to the log file
"""
from __future__ import annotations

import argparse
import logging
import sys
import traceback
from pathlib import Path

# Make sure the bundled package is importable when frozen or run from anywhere.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from craftymm import APP_TITLE, __version__  # noqa: E402
from craftymm.config import Settings, app_dir  # noqa: E402


def setup_logging(debug: bool) -> Path:
    log_path = app_dir() / "craftymm.log"
    handlers: list[logging.Handler] = [
        logging.FileHandler(log_path, encoding="utf-8"),
    ]
    if debug or sys.stdout is not None:
        handlers.append(logging.StreamHandler(sys.stdout))
    logging.basicConfig(
        level=logging.DEBUG if debug else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        handlers=handlers,
        force=True,
    )
    # paramiko and urllib3 are extremely chatty at DEBUG
    logging.getLogger("paramiko").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    return log_path


def main() -> int:
    parser = argparse.ArgumentParser(description=APP_TITLE)
    parser.add_argument("--debug", action="store_true", help="verbose logging")
    parser.add_argument("--version", action="store_true")
    args = parser.parse_args()

    if args.version:
        print(f"{APP_TITLE} {__version__}")
        return 0

    log_path = setup_logging(args.debug)
    log = logging.getLogger("craftymm")
    log.info("%s %s starting - log: %s", APP_TITLE, __version__, log_path)

    try:
        from PySide6.QtGui import QIcon
        from PySide6.QtWidgets import QApplication, QMessageBox
    except ImportError:
        sys.stderr.write(
            "PySide6 is not installed.\n\n"
            "Run  run.bat  (it sets everything up), or install it manually:\n"
            "    pip install -r requirements.txt\n"
        )
        return 2

    from craftymm.ui import theme
    from craftymm.ui.main_window import MainWindow

    app = QApplication(sys.argv)
    app.setApplicationName(APP_TITLE)
    app.setApplicationVersion(__version__)
    app.setStyle("Fusion")
    app.setStyleSheet(theme.STYLESHEET)

    icon_path = Path(__file__).resolve().parent / "assets" / "icon.png"
    if icon_path.exists():
        app.setWindowIcon(QIcon(str(icon_path)))

    # Never let a stray exception kill the window silently.
    def excepthook(exc_type, exc, tb):
        if issubclass(exc_type, KeyboardInterrupt):
            sys.__excepthook__(exc_type, exc, tb)
            return
        log.critical("unhandled exception", exc_info=(exc_type, exc, tb))
        QMessageBox.critical(
            None,
            "Unexpected error",
            f"{exc}\n\nThe full traceback is in:\n{log_path}",
        )

    sys.excepthook = excepthook

    settings = Settings.load()
    window = MainWindow(settings)
    window.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
