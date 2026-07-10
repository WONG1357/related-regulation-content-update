from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path


SUPPORTED_SUFFIXES = {".doc", ".docx", ".xlsx", ".xls"}


def discover_soffice() -> str | None:
    configured_soffice = os.getenv("CODEX_BUNDLED_SOFFICE", "").strip()
    candidates = [
        shutil.which("libreoffice"),
        shutil.which("soffice"),
        configured_soffice or None,
        str(Path.home() / ".cache" / "codex-runtimes" / "codex-primary-runtime" / "dependencies" / "bin" / "soffice"),
    ]
    for candidate in candidates:
        if candidate and Path(candidate).exists():
            return candidate
    return None


def convert_documents(input_dir: str | Path, output_dir: str | Path) -> dict[str, list[dict[str, str]]]:
    input_path = Path(input_dir).expanduser()
    output_path = Path(output_dir).expanduser()
    output_path.mkdir(parents=True, exist_ok=True)

    result: dict[str, list[dict[str, str]]] = {
        "converted_files": [],
        "ready_files": [],
        "failed_files": [],
        "ignored_files": [],
    }
    textutil = shutil.which("textutil")
    soffice = discover_soffice()

    soffice_disabled_reason = ""
    for file_path in sorted(input_path.iterdir()):
        if not file_path.is_file():
            continue
        if file_path.name.startswith("~$"):
            result["ignored_files"].append({"file_path": str(file_path), "reason": "temporary Word file"})
            continue
        suffix = file_path.suffix.lower()
        if suffix not in SUPPORTED_SUFFIXES:
            result["ignored_files"].append({"file_path": str(file_path), "reason": "unsupported file type"})
            continue
        if suffix in {".docx", ".xlsx", ".xls"}:
            result["ready_files"].append({"file_path": str(file_path), "source": "original"})
            continue

        converted = output_path / f"{file_path.stem}.docx"
        if converted.exists() and converted.stat().st_mtime_ns >= file_path.stat().st_mtime_ns:
            result["converted_files"].append({"file_path": str(converted), "source_file": str(file_path), "method": "existing"})
            continue
        if converted.exists():
            converted.unlink()
        errors = []

        if textutil:
            proc = subprocess.run(
                [textutil, "-convert", "docx", "-output", str(converted), str(file_path)],
                capture_output=True,
                text=True,
                timeout=60,
                check=False,
            )
            if converted.exists():
                result["converted_files"].append({"file_path": str(converted), "source_file": str(file_path), "method": "textutil"})
                continue
            errors.append((proc.stderr or proc.stdout or "textutil conversion failed").strip())

        if soffice and not soffice_disabled_reason:
            proc = subprocess.run(
                [soffice, "--headless", "--convert-to", "docx", "--outdir", str(output_path), str(file_path)],
                capture_output=True,
                text=True,
                timeout=20,
                check=False,
            )
            if converted.exists():
                result["converted_files"].append({"file_path": str(converted), "source_file": str(file_path), "method": "soffice"})
                continue
            soffice_error = (proc.stderr or proc.stdout or "soffice conversion failed").strip()
            errors.append(soffice_error)
            if "Library not loaded" in soffice_error or "liblcms2" in soffice_error:
                soffice_disabled_reason = soffice_error
        elif soffice_disabled_reason:
            errors.append(f"soffice skipped after prior failure: {soffice_disabled_reason}")

        result["failed_files"].append(
            {
                "file_path": str(file_path),
                "reason": " / ".join(err for err in errors if err) or "no converter available",
            }
        )
    return result
