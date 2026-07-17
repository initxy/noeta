"""Builtin-skill management API (admin console, prefix /skills, all gated by
require_admin).

Builtin-skill consolidation: builtins no longer ship with the code; they all
live in the shared directory `builtin-skills/<name>/` (backend-writable,
mounted read-only into sandboxes), and existence is authoritatively decided by
the `skills` table rows with `space_id="*"`. This API only creates / deletes /
updates those global builtin rows + the matching directories:

- list: read the table and list global builtins (with enabled).
- upload: parse the SKILL.md frontmatter name to decide the directory name
  (`^[A-Za-z0-9_-]{1,64}$`, which also blocks directory traversal), write
  `builtin-skills/<name>/` + write the row; re-uploading the same name =
  reinstall (overwrite the directory + INSERT OR REPLACE back to enabled by
  default).
- delete: really remove the directory + delete the row (builtins can now be
  truly deleted; no soft delete).
- patch: update the row's enabled flag (platform-wide effect; once disabled,
  new sessions do not assemble it).
- preview: read-only builtin content (the admin console needs to inspect it).

Space-scoped skills (upload) go through /spaces/{id}/skills; a space cannot
disable / delete builtins.
"""
from __future__ import annotations

import io
import re
import shutil
import tempfile
import zipfile
from pathlib import Path

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile
from pydantic import BaseModel

from noeta.agent.auth.deps import CurrentUser, require_admin
from noeta.agent.config import Settings
from noeta.agent.store.skills import GLOBAL_SPACE_ID, SkillStore

router = APIRouter(prefix="/skills", tags=["skills"])

_NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_PREVIEW_MAX_BYTES = 256 * 1024  # per-file preview cap: 256KB


# --------------------------------------------------------------- dependency access
def _settings(request: Request) -> Settings:
    return request.app.state.settings


def _skill_store(request: Request) -> SkillStore:
    return request.app.state.skill_store


def _builtin_root(request: Request) -> Path:
    return _settings(request).builtin_skills_path


# --------------------------------------------------------------- parsing / validation
def _parse_frontmatter(text: str) -> dict[str, str]:
    """Minimal YAML frontmatter extraction: only top-level `key: value`
    single-line scalars.

    Parses line by line between the first `---` and the next `---`; nested /
    multi-line values are unsupported (skill name/description in this repo are
    single-line). Returns an empty dict when nothing parses.
    """
    lines = text.splitlines()
    i = 0
    while i < len(lines) and lines[i].strip() == "":
        i += 1
    if i >= len(lines) or lines[i].strip() != "---":
        return {}
    out: dict[str, str] = {}
    for line in lines[i + 1:]:
        if line.strip() == "---":
            break
        m = re.match(r"^([A-Za-z0-9_-]+):\s*(.*)$", line)
        if not m:
            continue
        value = m.group(2).strip()
        if len(value) >= 2 and value[0] in "\"'" and value[-1] == value[0]:
            value = value[1:-1]
        out[m.group(1)] = value
    return out


def _validate_name(name: str) -> None:
    if not _NAME_RE.match(name):
        raise HTTPException(
            status_code=400,
            detail="skill names may only contain letters, digits, hyphens, and"
            " underscores, length <= 64",
        )


def _locate_skill_dir(root: Path) -> Path:
    """Locate the skill directory containing SKILL.md inside the extracted
    tree (pick the shallowest one)."""
    candidates = sorted(
        root.rglob("SKILL.md"), key=lambda p: len(p.relative_to(root).parts)
    )
    if not candidates:
        raise HTTPException(status_code=400, detail="no SKILL.md found in the zip")
    return candidates[0].parent


def _extract_zip(raw: bytes, dest: Path) -> None:
    """Safe extraction: reject absolute-path / `..` traversal members
    (zip slip)."""
    try:
        zf = zipfile.ZipFile(io.BytesIO(raw))
    except zipfile.BadZipFile:
        raise HTTPException(status_code=400, detail="not a valid zip file")
    root = dest.resolve()
    with zf:
        for member in zf.namelist():
            target = (dest / member).resolve()
            if target != root and not target.is_relative_to(root):
                raise HTTPException(
                    status_code=400, detail="zip contains an illegal path"
                )
        zf.extractall(dest)


def _row_view(row: dict) -> dict:
    """Builtin row -> API view (frontend Skill: name/description/source/enabled)."""
    return {
        "name": row["name"],
        "description": row["description"],
        "source": "builtin",
        "enabled": row["enabled"],
    }


# --------------------------------------------------------------- endpoints
@router.get("")
async def list_skills(
    request: Request, user: CurrentUser = Depends(require_admin)
) -> dict:
    """Global builtin-skill list (for the admin builtin-skill management page).
    Each entry carries `enabled`."""
    rows = _skill_store(request).list_builtin()
    return {"skills": [_row_view(r) for r in rows]}


@router.post("")
async def upload_skill(
    request: Request,
    file: UploadFile = File(...),
    user: CurrentUser = Depends(require_admin),
) -> dict:
    """Upload a builtin skill (.md / .zip): write `builtin-skills/<name>/` +
    write the global row. Re-uploading the same name = reinstall (overwrite the
    directory + back to enabled by default)."""
    root = _builtin_root(request)
    root.mkdir(parents=True, exist_ok=True)
    filename = (file.filename or "").lower()
    raw = await file.read()

    if filename.endswith(".md"):
        text = raw.decode("utf-8", errors="replace")
        fm = _parse_frontmatter(text)
        name = fm.get("name", "")
        description = fm.get("description", "")
        _validate_name(name)
        dest = root / name
        if dest.exists():
            shutil.rmtree(dest)
        dest.mkdir(parents=True)
        (dest / "SKILL.md").write_text(text, encoding="utf-8")
    elif filename.endswith(".zip"):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            _extract_zip(raw, tmp)
            src = _locate_skill_dir(tmp)
            skill_md_text = (src / "SKILL.md").read_text(
                encoding="utf-8", errors="replace"
            )
            fm = _parse_frontmatter(skill_md_text)
            name = fm.get("name", "")
            description = fm.get("description", "")
            _validate_name(name)
            dest = root / name
            if dest.exists():
                shutil.rmtree(dest)
            shutil.copytree(src, dest)
    else:
        raise HTTPException(
            status_code=400, detail="only .md or .zip files are supported"
        )

    # The directory is in place; write the global row (the existence
    # authority). On failure roll the directory back to avoid orphans.
    try:
        row = _skill_store(request).add(
            GLOBAL_SPACE_ID, name, source="builtin", description=description
        )
    except Exception:
        shutil.rmtree(dest, ignore_errors=True)
        raise
    return {"skill": _row_view(row)}


@router.delete("/{name}")
async def delete_skill(
    name: str, request: Request, user: CurrentUser = Depends(require_admin)
) -> dict:
    """Delete a builtin skill: really remove the directory + delete the row."""
    _validate_name(name)
    store = _skill_store(request)
    if store.get_builtin(name) is None:
        raise HTTPException(status_code=404, detail="skill not found")
    store.delete(GLOBAL_SPACE_ID, name)
    dest = _builtin_root(request) / name
    if dest.is_dir():
        shutil.rmtree(dest, ignore_errors=True)
    return {"ok": True}


class SkillToggleBody(BaseModel):
    enabled: bool


@router.patch("/{name}")
async def toggle_skill(
    name: str,
    body: SkillToggleBody,
    request: Request,
    user: CurrentUser = Depends(require_admin),
) -> dict:
    """Enable / disable a builtin skill globally (platform-wide effect; once
    disabled, new sessions do not assemble it; in-flight sessions are
    unaffected)."""
    _validate_name(name)
    store = _skill_store(request)
    if store.get_builtin(name) is None:
        raise HTTPException(status_code=404, detail="skill not found")
    store.set_enabled(GLOBAL_SPACE_ID, name, body.enabled)
    return {"ok": True, "name": name, "enabled": body.enabled}


# --------------------------------------------------------------- preview (read-only)
def _is_probably_binary(raw: bytes) -> bool:
    """Binary detection: a NUL byte or a utf-8 decode failure counts as
    binary."""
    if b"\x00" in raw:
        return True
    try:
        raw.decode("utf-8")
    except UnicodeDecodeError:
        return True
    return False


@router.get("/{name}/preview")
async def preview_skill(
    name: str,
    request: Request,
    path: str = "",
    user: CurrentUser = Depends(require_admin),
) -> dict:
    """Builtin-skill content preview (admin, read-only).

    - Without `path`: returns the file tree `{entries: [{path, size, is_dir}]}`
      (excluding `__MACOSX` / `._` junk).
    - With `path`: returns `{path, content, truncated, binary}` (text truncated
      to 256KB).

    Missing directory -> 404; a path escaping the skill directory -> 400.
    """
    _validate_name(name)
    store = _skill_store(request)
    if store.get_builtin(name) is None:
        raise HTTPException(status_code=404, detail="skill not found")
    skill_dir = _builtin_root(request) / name
    if not skill_dir.is_dir():
        raise HTTPException(status_code=404, detail="skill not found")
    root = skill_dir.resolve()

    if not path:
        entries: list[dict] = []
        for p in sorted(skill_dir.rglob("*")):
            parts = p.relative_to(skill_dir).parts
            if any(part == "__MACOSX" or part.startswith("._") for part in parts):
                continue
            rel = p.relative_to(skill_dir).as_posix()
            try:
                size = p.stat().st_size if p.is_file() else 0
            except OSError:
                size = 0
            entries.append({"path": rel, "size": size, "is_dir": p.is_dir()})
        return {"entries": entries}

    target = (skill_dir / path).resolve()
    if target != root and not target.is_relative_to(root):
        raise HTTPException(status_code=400, detail="invalid path")
    if not target.is_file():
        raise HTTPException(status_code=404, detail="file not found")
    raw = target.read_bytes()
    truncated = len(raw) > _PREVIEW_MAX_BYTES
    if truncated:
        raw = raw[:_PREVIEW_MAX_BYTES]
    if _is_probably_binary(raw):
        return {
            "path": path,
            "content": "(binary file, preview not supported)",
            "truncated": truncated,
            "binary": True,
        }
    return {
        "path": path,
        "content": raw.decode("utf-8", errors="replace"),
        "truncated": truncated,
        "binary": False,
    }
