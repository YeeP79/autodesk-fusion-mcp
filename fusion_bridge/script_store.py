"""Saved script management for user-authored Python snippets.

The `ScriptManager` class owns the scripts directory and provides CRUD
operations that return JSON payloads matching the public wire format.
"""

import os
from pathlib import Path

from .common.results import error_result, ok_result
from .dispatch import log


class ScriptManager:
    """Manage saved Python scripts under a given directory."""

    def __init__(self, base_dir=None):
        if base_dir is None:
            package_root = Path(os.path.dirname(os.path.abspath(__file__))).parent
            base_dir = package_root / "user_scripts"
        self._dir = Path(base_dir)
        self._dir.mkdir(parents=True, exist_ok=True)

    @property
    def directory(self):
        return str(self._dir)

    # -- path validation ---------------------------------------------------

    def _safe_path(self, filename):
        if not isinstance(filename, str) or not filename:
            raise ValueError("'filename' required")
        candidate = Path(filename)
        if (
            candidate.is_absolute()
            or candidate.name != filename
            or any(part == ".." for part in candidate.parts)
        ):
            raise ValueError("invalid filename")
        return self._dir / candidate.name

    # -- CRUD --------------------------------------------------------------

    def save(self, arguments):
        filename = arguments.get("filename")
        code = arguments.get("code")
        if not filename or code is None:
            return error_result("ERROR: 'filename' and 'code' required")
        try:
            target = self._safe_path(filename)
            target.write_text(code, encoding="utf-8")
            log(f"[MCP] Saved script: {target}")
            return ok_result(
                {
                    "filename": filename,
                    "path": str(target),
                    "size": len(code),
                    "saved": True,
                }
            )
        except Exception as exc:
            return error_result(f"ERROR saving script: {exc}")

    def load(self, arguments):
        filename = arguments.get("filename")
        if not filename:
            return error_result("ERROR: 'filename' required")
        try:
            target = self._safe_path(filename)
            if not target.exists():
                return error_result(f"ERROR: Script not found: {filename}")
            code = target.read_text(encoding="utf-8")
            log(f"[MCP] Loaded script: {target}")
            return ok_result(
                {
                    "filename": filename,
                    "code": code,
                    "size": len(code),
                    "path": str(target),
                }
            )
        except Exception as exc:
            return error_result(f"ERROR loading script: {exc}")

    def list_all(self, arguments):
        del arguments
        try:
            entries = []
            for name in sorted(os.listdir(self._dir)):
                if not name.endswith(".py"):
                    continue
                p = self._dir / name
                st = p.stat()
                entries.append(
                    {
                        "filename": name,
                        "size": st.st_size,
                        "modified": st.st_mtime,
                        "path": str(p),
                    }
                )
            log(f"[MCP] Listed {len(entries)} scripts")
            return ok_result(
                {
                    "scripts": entries,
                    "count": len(entries),
                    "directory": str(self._dir),
                }
            )
        except Exception as exc:
            return error_result(f"ERROR listing scripts: {exc}")

    def delete(self, arguments):
        filename = arguments.get("filename")
        if not filename:
            return error_result("ERROR: 'filename' required")
        try:
            target = self._safe_path(filename)
            if not target.exists():
                return error_result(f"ERROR: Script not found: {filename}")
            target.unlink()
            log(f"[MCP] Deleted script: {target}")
            return ok_result({"filename": filename, "deleted": True})
        except Exception as exc:
            return error_result(f"ERROR deleting script: {exc}")


# ── Module-level default instance and backwards-compat exports ───────────

_mgr = ScriptManager()

save_script = _mgr.save
load_script = _mgr.load
list_scripts = _mgr.list_all
delete_script = _mgr.delete

# Preserve helper used elsewhere
get_scripts_directory = lambda: _mgr.directory
