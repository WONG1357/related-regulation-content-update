import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_all, collect_submodules


project_root = Path(SPECPATH)
datas = [
    (str(project_root / "app.py"), "."),
    (str(project_root / "prompts"), "prompts"),
]
binaries = []
hiddenimports = collect_submodules("modules")
hiddenimports += ["pandas", "openpyxl", "docx", "dotenv", "requests"]
for package in ("streamlit", "webview"):
    package_datas, package_binaries, package_hiddenimports = collect_all(package)
    datas += package_datas
    binaries += package_binaries
    hiddenimports += package_hiddenimports

a = Analysis(
    [str(project_root / "desktop_launcher.py")],
    pathex=[str(project_root)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="RegulatoryReview",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
collection = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name="RegulatoryReview",
)

if sys.platform == "darwin":
    app = BUNDLE(
        collection,
        name="Regulatory Review.app",
        icon=None,
        bundle_identifier="com.mediconcepts.regulatory-review",
        info_plist={
            "CFBundleDisplayName": "Regulatory Review",
            "NSHighResolutionCapable": True,
        },
    )
