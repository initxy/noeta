"""Space-skill API: skill management at the space level.

Space skills live in shared_data_dir/space-skills/<space_id>/<skill_name>/
(backend-writable, visible to sandboxes through the read-only /data mount).
The API reuses the frontmatter parsing / name validation / zip anti-traversal
logic from the existing skills.py.

Permissions: space members can read; only the owner can upload/delete.
"""
from __future__ import annotations

import io
import re
import shutil
import tempfile
import zipfile
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile
from pydantic import BaseModel

from noeta.agent.auth.deps import CurrentUser, get_current_user
from noeta.agent.config import Settings
from noeta.agent.store.skills import SkillStore
from noeta.agent.store.spaces import ROLE_OWNER, SpaceStore

router = APIRouter(prefix="/spaces/{space_id}/skills", tags=["space-skills"])

_NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_GROUP_MAX_LEN = 32


def _space_store(request: Request) -> SpaceStore:
    return request.app.state.space_store


def _settings(request: Request) -> Settings:
    return request.app.state.settings


def _skill_store(request: Request) -> SkillStore:
    return request.app.state.skill_store


def _membership_or_404(
    request: Request, space_id: str, user: CurrentUser
) -> tuple[dict, str]:
    store = _space_store(request)
    space = store.get_space(space_id)
    role = store.get_member_role(space_id, user.username) if space else None
    if space is None or role is None:
        raise HTTPException(status_code=404, detail="space not found")
    return space, role


def _require_owner(role: str) -> None:
    if role != ROLE_OWNER:
        raise HTTPException(status_code=403, detail="space owner permission required")


def _parse_frontmatter(text: str) -> dict[str, str]:
    """Minimal YAML frontmatter extraction (same as skills.py)."""
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


def _normalize_group(group: Optional[str]) -> Optional[str]:
    """Validate and normalize a group name: None / blank -> None (remove from
    the group); otherwise strip surrounding whitespace, length <= 32, no
    control characters (CJK / spaces allowed)."""
    if group is None:
        return None
    g = group.strip()
    if not g:
        return None
    if len(g) > _GROUP_MAX_LEN:
        raise HTTPException(
            status_code=400,
            detail=f"group names must not exceed {_GROUP_MAX_LEN} characters",
        )
    if any(ord(c) < 0x20 for c in g):
        raise HTTPException(
            status_code=400, detail="group names must not contain control characters"
        )
    return g


def _space_skills_dir(settings: Settings, space_id: str) -> Path:
    return settings.space_skills_path / space_id


def _extract_zip(raw: bytes, dest: Path) -> None:
    """Safe extraction (same as skills.py)."""
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


def _locate_skill_dir(root: Path) -> Path:
    candidates = sorted(root.rglob("SKILL.md"), key=lambda p: len(p.relative_to(root).parts))
    if not candidates:
        raise HTTPException(status_code=400, detail="no SKILL.md found in the zip")
    return candidates[0].parent


def assemble_space_skills(request: Request, space_id: str) -> list[dict]:
    """Assemble the space skill list: global builtins (read-only display) +
    this space's skills (uploads).

    Shared by the member endpoints and the admin console — admin goes through
    the same assembly, bypassing the membership check and checking only admin.
    Both segments read the `skills` table (pure SELECT, no directory scan):
    builtins take the enabled rows with `space_id="*"`, pinned to the
    "builtin" group (group always None) and `enabled` always True (a space
    cannot turn builtins off; read-only); space skills take this space's rows.
    """
    store = _skill_store(request)
    skills: list[dict] = []

    # Global builtins: list only the enabled ones (globally disabled ones are
    # simply not shown -> the list = the skills sessions actually assemble).
    for row in store.list_builtin():
        if not row["enabled"]:
            continue
        skills.append(
            {
                "name": row["name"],
                "description": row["description"],
                "source": "builtin",
                "enabled": True,
                "group": None,
            }
        )

    # Space skills: read the registry (pure SELECT, no directory scan).
    for row in store.list_by_space(space_id):
        entry = {
            "name": row["name"],
            "description": row["description"],
            "source": row["source"],
            "enabled": row["enabled"],
            "group": row["group"],
            "installed_at": row["installed_at"],
        }
        skills.append(entry)

    return skills


@router.get("")
async def list_space_skills(
    space_id: str,
    request: Request,
    user: CurrentUser = Depends(get_current_user),
) -> dict:
    _membership_or_404(request, space_id, user)
    return {"skills": assemble_space_skills(request, space_id)}


@router.post("", status_code=201)
async def upload_space_skill(
    space_id: str,
    request: Request,
    file: UploadFile = File(...),
    user: CurrentUser = Depends(get_current_user),
) -> dict:
    _space, role = _membership_or_404(request, space_id, user)
    _require_owner(role)

    settings = _settings(request)
    space_dir = _space_skills_dir(settings, space_id)
    space_dir.mkdir(parents=True, exist_ok=True)

    filename = (file.filename or "").lower()
    raw = await file.read()

    if filename.endswith(".md"):
        text = raw.decode("utf-8", errors="replace")
        fm = _parse_frontmatter(text)
        name = fm.get("name", "")
        description = fm.get("description", "")
        _validate_name(name)
        # Check for a name collision with a global builtin (builtins win;
        # assembly would skip the same-named space skill -> reject the upload
        # outright).
        if _skill_store(request).get_builtin(name) is not None:
            raise HTTPException(
                status_code=409,
                detail=f"a builtin skill with this name already exists: {name}",
            )
        dest = space_dir / name
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
            if _skill_store(request).get_builtin(name) is not None:
                raise HTTPException(
                    status_code=409,
                    detail=f"a builtin skill with this name already exists: {name}",
                )
            dest = space_dir / name
            if dest.exists():
                shutil.rmtree(dest)
            shutil.copytree(src, dest)
    else:
        raise HTTPException(
            status_code=400, detail="only .md or .zip files are supported"
        )

    # The directory is in place; write the registry row (re-upload =
    # reinstall: INSERT OR REPLACE back to enabled by default, group cleared).
    # If writing the row fails, roll the directory back to avoid a
    # "directory without a row" orphan (assembly / listing both trust the
    # rows, so an orphan directory is never assembled and never listed, but we
    # still clean it up for consistency).
    try:
        skill = _skill_store(request).add(
            space_id, name, source="upload", description=description
        )
    except Exception:
        shutil.rmtree(dest, ignore_errors=True)
        raise
    return {
        "skill": {
            "name": skill["name"],
            "description": skill["description"],
            "source": skill["source"],
            "enabled": skill["enabled"],
            "group": skill["group"],
            "installed_at": skill["installed_at"],
        }
    }


@router.delete("/{name}")
async def delete_space_skill(
    space_id: str,
    name: str,
    request: Request,
    user: CurrentUser = Depends(get_current_user),
) -> dict:
    _space, role = _membership_or_404(request, space_id, user)
    _require_owner(role)

    _validate_name(name)
    settings = _settings(request)
    store = _skill_store(request)
    row = store.get(space_id, name)
    if row is None:
        raise HTTPException(status_code=404, detail="skill not found")
    # Deletion: delete the registry row first (the existence authority — once
    # deleted, the skill disappears from listing / assembly), then clear the
    # directory. A failed rmtree leaves at worst an orphan directory: it never
    # appears in any list and is never assembled, so it is harmless.
    store.delete(space_id, name)
    dest = _space_skills_dir(settings, space_id) / name
    if dest.is_dir():
        shutil.rmtree(dest, ignore_errors=True)
    return {"ok": True}


# --------------------------------------------------------------- enable toggle


class SkillToggleBody(BaseModel):
    enabled: bool


@router.patch("/{name}")
async def toggle_skill(
    space_id: str,
    name: str,
    body: SkillToggleBody,
    request: Request,
    user: CurrentUser = Depends(get_current_user),
) -> dict:
    """Enable / disable this space's skill (owner only). Once disabled, new
    sessions no longer assemble the skill; in-flight sessions are unaffected.
    Only applies to space skills (uploads); builtins are global and a space
    cannot disable them (not a row of this space -> 404); disabling builtins
    goes through the admin /skills."""
    _space, role = _membership_or_404(request, space_id, user)
    _require_owner(role)
    _validate_name(name)
    if _skill_store(request).get(space_id, name) is None:
        raise HTTPException(status_code=404, detail="skill not found")
    _skill_store(request).set_enabled(space_id, name, body.enabled)
    return {"ok": True, "name": name, "enabled": body.enabled}


# --------------------------------------------------------------- user groups
#
# Groups are a pure display-layer organizing device (the frontend renders
# collapsible groups); they do not affect assembly. They apply uniformly by
# directory name to uploaded space skills; builtins are pinned to the
# "builtin" group and cannot be changed (a builtin is not a row of this space
# -> 404). Groups have no standalone entity: clearing group removes the skill
# from its group, and deleting a group's last member makes the group
# disappear. Batch grouping is done by the frontend calling this endpoint
# concurrently for multiple names (reusing the single-item endpoint; no batch
# body is introduced).


class SkillGroupBody(BaseModel):
    # None / empty string = remove from the group; otherwise, after stripping
    # whitespace, length <= 32 and no control characters (CJK allowed).
    group: Optional[str] = None


@router.put("/{name}/group")
async def set_skill_group(
    space_id: str,
    name: str,
    body: SkillGroupBody,
    request: Request,
    user: CurrentUser = Depends(get_current_user),
) -> dict:
    """Assign an installed skill to a group / remove it from its group (owner
    only). Builtins are global and cannot be grouped (not a row of this space
    -> 404)."""
    _space, role = _membership_or_404(request, space_id, user)
    _require_owner(role)
    _validate_name(name)
    if _skill_store(request).get(space_id, name) is None:
        raise HTTPException(status_code=404, detail="skill not found")
    group = _normalize_group(body.group)
    _skill_store(request).set_group(space_id, name, group)
    return {"ok": True, "name": name, "group": group}


# --------------------------------------------------------------- preview (read-only)
#
# Installed skills (space uploads, landing in space-skills/<space_id>/<name>/)
# get a read-only preview: without path it returns the file tree, with path it
# returns file contents. Builtins are global and do not land in the space
# directory, so the space side offers no preview (the frontend shows no entry
# point, and the backend naturally 404s on the missing directory); previewing
# builtins goes through the admin /skills/{name}/preview. name goes through
# _validate_name (blocking directory traversal) and path through an
# is_relative_to check, ensuring nothing escapes the skill directory.

_PREVIEW_MAX_BYTES = 256 * 1024  # per-file preview cap: 256KB


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
    space_id: str,
    name: str,
    request: Request,
    path: str = "",
    user: CurrentUser = Depends(get_current_user),
) -> dict:
    """Space-skill preview (read-only, members can read).

    - Without `path`: returns the file tree `{entries: [{path, size, is_dir}]}`
      (relative to the skill directory, excluding `__MACOSX` / `._` junk).
    - With `path`: returns `{path, content, truncated, binary}` (text truncated
      to `_PREVIEW_MAX_BYTES`; binaries return a placeholder, no content).

    Missing directory -> 404 (builtins do not land in the space directory and
    naturally 404; preview builtins through the admin /skills); a path
    escaping the skill directory -> 400.
    """
    _membership_or_404(request, space_id, user)
    _validate_name(name)
    settings = _settings(request)
    skill_dir = _space_skills_dir(settings, space_id) / name
    if not skill_dir.is_dir():
        raise HTTPException(status_code=404, detail="skill not found")
    root = skill_dir.resolve()

    if not path:
        entries: list[dict] = []
        for p in sorted(skill_dir.rglob("*")):
            # Exclude system junk.
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

    # File contents.
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
