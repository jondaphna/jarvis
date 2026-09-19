"""Entry point for the packaged app (PyInstaller starts here).

Opens the desktop window. If PyQt6 isn't available it falls back to the
terminal, so a stripped-down build still works.
"""

import multiprocessing
import sys

if __name__ == "__main__":
    multiprocessing.freeze_support()   # required for frozen Windows builds

    from jarvis.cli import main

    # No arguments: `jarvis` opens the UI, falling back to chat.
    sys.exit(main(sys.argv[1:] or None))
