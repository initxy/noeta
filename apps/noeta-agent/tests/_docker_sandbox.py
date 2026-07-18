"""Gate for docker-gated sandbox e2e: skip without a docker daemon / a local
AIO image.

The image name can be set via env ``SANDBOX_TEST_IMAGE``; otherwise common
names are probed (a locally built custom image + the public ghcr one). Only an
image that ``docker image inspect`` hits counts as available (we never pull a
10GB image on our own).
"""
from __future__ import annotations

import os
import shutil
import subprocess

import pytest


def _find_image() -> str | None:
    env = os.environ.get("SANDBOX_TEST_IMAGE")
    candidates = ([env] if env else []) + [
        "noeta-agent-sandbox:latest",
        "ghcr.io/agent-infra/sandbox:latest",
    ]
    if not shutil.which("docker"):
        return None
    for c in candidates:
        if not c:
            continue
        try:
            r = subprocess.run(
                ["docker", "image", "inspect", c],
                capture_output=True, timeout=15,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        if r.returncode == 0:
            return c
    return None


DOCKER_SANDBOX_IMAGE = _find_image()

requires_docker_sandbox = pytest.mark.skipif(
    DOCKER_SANDBOX_IMAGE is None,
    reason="docker-gated: needs a docker daemon + a local AIO sandbox image"
    " (set SANDBOX_TEST_IMAGE to name the image)",
)
