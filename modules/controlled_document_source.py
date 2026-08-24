from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from pathlib import Path, PurePosixPath
from typing import Any, Iterable


SUPPORTED_CONTROLLED_SUFFIXES = {".doc", ".docx", ".xls", ".xlsx"}


class ControlledDocumentSourceError(ValueError):
    """Raised when a controlled-document source cannot be used safely."""


def save_uploaded_controlled_documents(uploaded_files: Iterable[Any], upload_root: str | Path) -> dict[str, Any]:
    files: list[tuple[str, bytes]] = []
    names_seen: dict[str, str] = {}
    for uploaded in uploaded_files:
        name = safe_uploaded_basename(getattr(uploaded, "name", ""))
        if not name or name.startswith("~$"):
            continue
        if Path(name).suffix.lower() not in SUPPORTED_CONTROLLED_SUFFIXES:
            raise ControlledDocumentSourceError(f"Unsupported controlled-document file type: {name}")
        duplicate = names_seen.get(name.casefold())
        if duplicate:
            raise ControlledDocumentSourceError(
                f"The selected folder contains duplicate filenames after flattening subfolders: {duplicate} and {name}."
            )
        names_seen[name.casefold()] = name
        files.append((name, bytes(uploaded.getvalue())))

    if not files:
        raise ControlledDocumentSourceError(
            "The selected folder contains no supported controlled documents (.doc, .docx, .xls, or .xlsx)."
        )

    digest = hashlib.sha256()
    file_records = []
    for name, content in sorted(files, key=lambda item: item[0].casefold()):
        file_digest = hashlib.sha256(content).hexdigest()
        digest.update(name.casefold().encode("utf-8"))
        digest.update(b"\0")
        digest.update(file_digest.encode("ascii"))
        file_records.append({"file_name": name, "size": len(content), "sha256": file_digest})
    source_id = digest.hexdigest()[:16]

    root = Path(upload_root)
    source_dir = root / source_id / "files"
    source_dir.mkdir(parents=True, exist_ok=True)
    for name, content in files:
        destination = source_dir / name
        if not destination.is_file() or file_sha256(destination) != hashlib.sha256(content).hexdigest():
            destination.write_bytes(content)

    manifest = {
        "source_id": source_id,
        "source_mode": "uploaded_directory",
        "source_label": "Uploaded controlled-document folder",
        "file_count": len(files),
        "total_bytes": sum(len(content) for _, content in files),
        "files": file_records,
    }
    manifest_path = root / source_id / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return {**manifest, "directory": str(source_dir)}


def describe_configured_controlled_documents(directory: str | Path) -> dict[str, Any]:
    path = Path(directory)
    if not path.is_dir():
        raise ControlledDocumentSourceError(
            "The configured controlled-document folder is unavailable. Upload the folder in the app instead."
        )
    files = [
        item
        for item in path.iterdir()
        if item.is_file()
        and not item.name.startswith("~$")
        and item.suffix.lower() in SUPPORTED_CONTROLLED_SUFFIXES
    ]
    if not files:
        raise ControlledDocumentSourceError(
            "The configured controlled-document folder contains no supported files. Upload a valid folder instead."
        )
    digest = hashlib.sha256(str(path.resolve()).encode("utf-8"))
    for item in sorted(files, key=lambda candidate: candidate.name.casefold()):
        stat = item.stat()
        digest.update(item.name.casefold().encode("utf-8"))
        digest.update(b"\0")
        digest.update(str(stat.st_size).encode("ascii"))
        digest.update(b"\0")
        digest.update(str(stat.st_mtime_ns).encode("ascii"))
    source_id = digest.hexdigest()[:16]
    return {
        "source_id": source_id,
        "source_mode": "configured_server_folder",
        "source_label": f"Configured server folder: {path.name}",
        "file_count": len(files),
        "total_bytes": sum(item.stat().st_size for item in files),
        "directory": str(path.resolve()),
    }


def safe_uploaded_basename(value: object) -> str:
    normalized = unicodedata.normalize("NFKC", str(value or "")).replace("\\", "/")
    name = PurePosixPath(normalized).name.strip()
    name = re.sub(r"[\x00-\x1f\x7f]", "_", name)
    if name in {"", ".", ".."}:
        return ""
    return name[:240]


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


__all__ = [
    "ControlledDocumentSourceError",
    "SUPPORTED_CONTROLLED_SUFFIXES",
    "describe_configured_controlled_documents",
    "safe_uploaded_basename",
    "save_uploaded_controlled_documents",
]
