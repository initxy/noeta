"""Images the person pastes into a message, also saved as files in the
task's exec env, with a note naming where (Claude Code's pasted-image
paths): the model sees the image either way, but only a file can be copied
into the repository or passed to a command.

Saved once per message (what was handled is recorded, in the log): on a
local exec env under ``<noeta home>/image-cache/<task>/``, elsewhere under
``/tmp/noeta-image-cache/<task>/`` inside the env.
"""

from __future__ import annotations

import posixpath

from noeta.core.extension import TaskView
from noeta.core.large_output import file_name_part
from noeta.core.types import Image, Message, Text
from noeta.plugins.fs.mentions import new_prompts
from noeta.plugins.fs.state import FS, KIND_IMAGES, current_fs_state
from noeta.toolkit.local_env import is_local
from noeta.toolkit.paths import noeta_home

__all__ = ["REMOTE_IMAGE_DIR", "image_note", "save_pasted_images"]

#: Where an exec env that is not this machine keeps the files.
REMOTE_IMAGE_DIR = "/tmp/noeta-image-cache"

_ONE = ("The image attached above is also saved as a file: {paths}. Use that path only when "
        "the task needs the file itself (to copy it into the project, say); you can already "
        "see the image, so there is no need to read the file to look at it.")
_MANY = ("The {n} images attached above are also saved as files, in the order shown: {paths}. "
         "Use those paths only when the task needs the files themselves (to copy one into the "
         "project, say); you can already see the images, so there is no need to read them to "
         "look at them.")

_EXT = {"image/png": "png", "image/jpeg": "jpg", "image/gif": "gif", "image/webp": "webp"}


def image_note(paths: list[str]) -> str:
    quoted = ", ".join(f'"{p}"' for p in paths)
    return _ONE.format(paths=quoted) if len(paths) == 1 else _MANY.format(n=len(paths),
                                                                          paths=quoted)


def _image_dir(view: TaskView) -> str:
    task = file_name_part(view.task_id, "task")
    if is_local(view.env):
        return str(noeta_home() / "image-cache" / task)
    return posixpath.join(REMOTE_IMAGE_DIR, task)


async def save_pasted_images(view: TaskView) -> None:
    """Save the images of the person's new messages and record a note per
    message naming the files; an image that cannot be written is left out."""
    env = view.env
    if env is None:
        return
    prompts = new_prompts(view, current_fs_state(view.state.ext).images_through)
    if not any(isinstance(b, Image) for _, m in prompts for b in m.content):
        return
    folder = _image_dir(view)
    for seq, message in prompts:
        images = [b for b in message.content if isinstance(b, Image)]
        paths: list[str] = []
        for i, image in enumerate(images, 1):
            path = posixpath.join(folder, f"{seq}-{i}.{_EXT.get(image.media_type, 'img')}")
            try:
                await env.write_bytes(path, image.data)
            except Exception:  # noqa: BLE001 — a file is a nicety; the image is shown anyway
                continue
            paths.append(path)
        view.record(FS, KIND_IMAGES, {"through": seq, "paths": paths},
                    message=Message(role="user", content=[Text(text=image_note(paths))],
                                    origin="system") if paths else None)
