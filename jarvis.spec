# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller build for JARVIS.

Built as a one-folder app rather than --onefile on purpose: the local Whisper
model and PyQt6 make a single-file build slow to start (it unpacks to temp on
every launch) and awkward to antivirus-whitelist. The folder starts instantly
and can still be zipped and handed to someone.
"""

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

hidden = []
for package in ("anthropic", "apscheduler", "edge_tts", "faster_whisper",
                "sounddevice", "soundfile", "jarvis.plugins"):
    try:
        hidden += collect_submodules(package)
    except Exception:
        pass   # an optional dependency that isn't installed

datas = [("jarvis/builtin_missions", "jarvis/builtin_missions"),
         ("jarvis/plugins", "jarvis/plugins")]
for package in ("anthropic", "certifi"):
    try:
        datas += collect_data_files(package)
    except Exception:
        pass

a = Analysis(
    ["run_jarvis.py"],
    pathex=[],
    binaries=[],
    datas=datas,
    hiddenimports=hidden,
    hookspath=[],
    runtime_hooks=[],
    excludes=["tkinter", "matplotlib", "pandas", "scipy", "IPython", "pytest"],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz, a.scripts, [],
    exclude_binaries=True,
    name="JARVIS",
    debug=False,
    strip=False,
    upx=True,
    console=False,          # no terminal window; logs go to the log file
    icon="assets/icon.ico" if __import__("os").path.exists("assets/icon.ico") else None,
)

coll = COLLECT(
    exe, a.binaries, a.datas,
    strip=False, upx=True, upx_exclude=[],
    name="JARVIS",
)
