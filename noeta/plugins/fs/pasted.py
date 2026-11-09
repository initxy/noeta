"""Images the person pastes into a message, also saved as files in the
task's exec env, each named to the model in one line (``[Image source:
<path>]``, Claude Code's form): the model sees the image either way, but
only a file can be copied into the repository or passed to a command.

Where: on a local exec env ``<noeta home>/image-cache/<task>/`` (a sandboxed
command may read it), elsewhere ``<temp>/noeta-images-<uid>/<task>/`` inside
the env. Directories are made 0700 and must be real directories owned by
the user, files are created 0600 and never through a symlink, all by one
script run in the env (so a container is written from inside).

Saved once per message (recorded in the log). At the start of each later
turn the recorded files are checked: one that is gone (another worker, a
cleaned temp directory) is written again from the image in the
conversation, and when that cannot be done, or lands elsewhere, the model is
told. A root task's directory is removed when it ends, and task directories
untouched for :data:`IMAGE_KEEP_S` go when a worker first saves an image
(Claude Code clears other sessions' image caches when it starts).
"""

from __future__ import annotations

import logging
import os
from collections.abc import Sequence

from noeta.core.events import Recorded, TaskEnded
from noeta.core.extension import TaskView
from noeta.core.large_output import file_name_part
from noeta.core.types import Image, Message, Text
from noeta.plugins.fs.mentions import new_prompts
from noeta.plugins.fs.state import FS, KIND_IMAGES, current_fs_state
from noeta.toolkit.local_env import is_local
from noeta.toolkit.paths import noeta_home

__all__ = ["IMAGE_KEEP_S", "image_note", "remove_task_images", "save_pasted_images"]

log = logging.getLogger(__name__)

#: Task directories untouched this long are removed (a later turn of such a
#: task writes its images again).
IMAGE_KEEP_S = 7 * 24 * 3600
#: Recorded images checked at the start of a turn, newest first.
CHECKED_MESSAGES = 20

_EXT = {"image/png": "png", "image/jpeg": "jpg", "image/gif": "gif", "image/webp": "webp"}

#: ``$1`` the base directory (empty: ``<temp>/noeta-images-<uid>``, ``<temp>``
#: the first usable of ``$2``, ``$TMPDIR``, ``/var/tmp``, ``/tmp``); ``$3`` the
#: task's directory name; ``$4`` the action: ``save <name>`` (stdin: the
#: bytes; an existing regular file is kept), ``sweep <minutes>`` or ``remove``.
_SCRIPT = r"""
d=$1
if [ -z "$d" ]; then
  b=/tmp
  for c in "$2" "${TMPDIR:-}" /var/tmp; do
    if [ -n "$c" ] && [ -d "$c" ] && [ -w "$c" ]; then b=$c; break; fi
  done
  d="$b/noeta-images-$(id -u)"
fi
mine() { [ ! -L "$1" ] && [ -d "$1" ] && [ -O "$1" ]; }
case $4 in
remove) if mine "$d" && mine "$d/$3"; then rm -rf -- "$d/$3"; fi; exit 0 ;;
sweep)
  if mine "$d"; then
    find "$d" -mindepth 1 -maxdepth 1 -type d -mmin "+$5" -exec rm -rf -- {} + 2>/dev/null
  fi
  exit 0 ;;
esac
for p in "$d" "$d/$3"; do
  if [ ! -e "$p" ] && [ ! -L "$p" ]; then (umask 077 && mkdir -p -- "$p") || exit 3; fi
  if ! mine "$p"; then echo "refusing $p: not a directory owned by the current user" >&2; exit 4; fi
  chmod -- 700 "$p" || exit 5  # the order BSD chmod takes too
done
f="$d/$3/$5"
if [ -L "$f" ]; then echo "refusing $f: a symlink" >&2; exit 4; fi
if [ ! -f "$f" ]; then (umask 077 && set -C && cat > "$f") || exit 6; fi
printf '%s' "$f"
"""

_swept: set[str] = set()
_warned: set[str] = set()


def image_note(paths: Sequence[str]) -> str:
    return "\n".join(f"[Image source: {p}]" for p in paths)


def _base(view: TaskView) -> tuple[str, str]:
    """The base directory argument and the temp directory hint."""
    if is_local(view.env):
        return str(noeta_home() / "image-cache"), os.environ.get("TMPDIR") or ""
    return "", ""


def _task_dir(view: TaskView) -> str:
    return file_name_part(view.state.root_id or view.task_id, "task")


async def _run(view: TaskView, action: str, arg: str, data: bytes | None = None) -> str | None:
    assert view.env is not None
    base, tmp = _base(view)
    try:
        result = await view.env.run(["sh", "-c", _SCRIPT, "sh", base, tmp, _task_dir(view),
                                     action, arg], stdin=data, timeout=30, output_cap=8192)
    except Exception as exc:  # noqa: BLE001 — a file is a nicety; the image is shown anyway
        reason = str(exc)
    else:
        if result.exit_code == 0:
            return result.stdout.decode("utf-8", "replace")
        reason = result.stderr.decode("utf-8", "replace").strip() or f"exit {result.exit_code}"
    if reason not in _warned:
        _warned.add(reason)
        log.warning("pasted image not saved: %s", reason)
    return None


async def _save(view: TaskView, seq: int, images: list[Image]) -> list[str]:
    key = f"{is_local(view.env)}:{_base(view)[0]}"
    if key not in _swept:
        _swept.add(key)
        await _run(view, "sweep", str(IMAGE_KEEP_S // 60))
    paths = []
    for i, image in enumerate(images, 1):
        name = f"{seq}-{i}.{_EXT.get(image.media_type, 'img')}"
        path = await _run(view, "save", name, image.data)
        if path:
            paths.append(path)
    return paths


def _note(paths: Sequence[str]) -> Message:
    return Message(role="user", content=[Text(text=image_note(paths))], origin="system")


async def save_pasted_images(view: TaskView) -> None:
    """Save the images of the person's new messages and record a note per
    message naming the files; check the earlier ones at a turn's start."""
    if view.env is None:
        return
    st = current_fs_state(view.state.ext)
    if not view.state.steps_in_turn:
        await _check_saved(view, dict(st.images))
    for seq, message in new_prompts(view, st.images_through):
        images = [b for b in message.content if isinstance(b, Image)]
        if not images:
            continue
        paths = await _save(view, seq, images)
        view.record(FS, KIND_IMAGES, {"through": seq, "paths": paths},
                    message=_note(paths) if paths else None)


async def _check_saved(view: TaskView, saved: dict[str, list[str]]) -> None:
    """Write again the recorded files that are gone; tell the model where
    they are now, or that they are no longer there."""
    assert view.env is not None
    by_seq = dict(zip(view.state.message_seqs, view.state.messages, strict=False))
    for key in sorted(saved, key=int, reverse=True)[:CHECKED_MESSAGES]:
        paths = saved[key]
        missing = False
        for path in paths:
            try:
                missing = missing or await view.env.stat(path) is None
            except Exception:  # noqa: BLE001 — unknown counts as gone
                missing = True
        if not missing:
            continue
        message = by_seq.get(int(key))
        images = [b for b in message.content if isinstance(b, Image)] if message else []
        again = await _save(view, int(key), images) if images else []
        if again == paths:
            continue
        text = image_note(again) if again else (
            "The image file(s) " + ", ".join(paths) + " saved earlier are no longer available.")
        view.record(FS, KIND_IMAGES, {"through": int(key), "paths": again},
                    message=Message(role="user", content=[Text(text=text)], origin="system"))


async def remove_task_images(view: TaskView, recorded: Recorded) -> None:
    """When a root task ends, its image directory goes."""
    if not isinstance(recorded.event, TaskEnded) or view.state.parent_id is not None \
            or view.env is None or not current_fs_state(view.state.ext).images:
        return
    await _run(view, "remove", "")
