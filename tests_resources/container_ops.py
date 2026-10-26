"""
General container build/run utilities for test suites.
Works with ANY Dockerfile (L4T, PyTorch, TensorRT, Ubuntu, etc.).

Key principle: Dockerfiles only COMPILE, never RUN tests.
Test execution happens via run_container() so each test gets individual pass/fail.
"""
import os
import logging
import shlex
from datetime import datetime

logger = logging.getLogger(__name__)

# RHEL/Podman GPU flags (CDI-based, replaces Docker's --runtime nvidia)
PODMAN_GPU_FLAGS = "--device nvidia.com/gpu=all --group-add keep-groups --security-opt label=disable --net=host"


def podman_command(arguments: str = "") -> str:
    """Build a remote Podman command with optional test storage settings.

    The environment variables are intentionally evaluated at call time so
    callers can configure them before creating an SSH fixture.  TMPDIR is
    separate from Podman's graphroot: image pulls use both locations.
    """
    parts = []
    tmpdir = os.getenv("JETSON_PODMAN_TMPDIR")
    if tmpdir:
        parts.append(f"TMPDIR={shlex.quote(tmpdir)}")

    parts.append("podman")
    root = os.getenv("JETSON_PODMAN_ROOT")
    runroot = os.getenv("JETSON_PODMAN_RUNROOT")
    if root:
        parts.extend(["--root", shlex.quote(root)])
    if runroot:
        parts.extend(["--runroot", shlex.quote(runroot)])
    if arguments:
        parts.append(arguments)
    return " ".join(parts)

# Configurable L4T image tag — overridden at session start by set_l4t_image_from_version()
L4T_JETPACK_IMAGE = os.getenv("L4T_JETPACK_IMAGE", "nvcr.io/nvidia/l4t-jetpack:r36.4.0")

# NGC l4t-jetpack version tags (image tags only — ignore .sig / SBOM / VEX).
# There is no r36.5.x (JetPack 6.2.x) and no r39.x (JetPack 7 / RHEL 10.2).
# Newest host driver + older container userspace is NVIDIA's supported combo.
# Keep newest-first; add a tag here when NVIDIA publishes it. (https://catalog.ngc.nvidia.com/orgs/nvidia/-/containers/l4t-jetpack/)
PUBLISHED_L4T_JETPACK_TAGS = (
    "r36.4.0",  # JetPack 6.x latest — use this for RHEL 9.8 / host L4T 36.5.x
    "r36.3.0",
    "r36.2.0",
    "r35.4.1",  # JetPack 5.x
    "r35.3.1",
    "r35.2.1",
    "r35.1.0",
)


def _parse_l4t_tuple(version) -> tuple:
    text = str(version).lstrip("rR")
    parts = []
    for p in text.split("."):
        try:
            parts.append(int(p))
        except ValueError:
            break
    while len(parts) < 3:
        parts.append(0)
    return tuple(parts[:3])


def resolve_l4t_jetpack_tag(l4t_version: str) -> str:
    """Pick a published NGC l4t-jetpack tag for the device's L4T version.

    Prefer an exact tag when NGC has it. Otherwise use the newest published
    tag on the same L4T major that is still <= the host (newer host driver
    can run older container userspace).
    """
    want = _parse_l4t_tuple(l4t_version)
    exact = "r" + str(l4t_version).lstrip("rR")
    if exact in PUBLISHED_L4T_JETPACK_TAGS:
        return exact

    same_major_le = []
    same_major = []
    for tag in PUBLISHED_L4T_JETPACK_TAGS:
        parsed = _parse_l4t_tuple(tag)
        if parsed[0] != want[0]:
            continue
        same_major.append(tag)
        if parsed <= want:
            same_major_le.append((parsed, tag))
    if same_major_le:
        return max(same_major_le)[1]
    if same_major:
        return same_major[0]
    # No published tag on this L4T major (e.g. host r39.x / JetPack 7).
    # Fall back to the newest NGC image so the pull is attempted, not skipped.
    return PUBLISHED_L4T_JETPACK_TAGS[0]


def get_l4t_jetpack_image() -> str:
    """Current L4T_JETPACK_IMAGE (read at call time, not import time)."""
    return L4T_JETPACK_IMAGE


def set_l4t_image_from_version(l4t_version: str) -> None:
    """Set L4T_JETPACK_IMAGE from device L4T, mapped to a published NGC tag.

    No-op when L4T_JETPACK_IMAGE is already set via the environment.
    Host L4T 36.5.x has no NGC container; r36.4.0 is the compatible tag.
    """
    global L4T_JETPACK_IMAGE
    if "L4T_JETPACK_IMAGE" in os.environ:
        return
    tag = resolve_l4t_jetpack_tag(l4t_version)
    L4T_JETPACK_IMAGE = f"nvcr.io/nvidia/l4t-jetpack:{tag}"
    exact = "r" + str(l4t_version).lstrip("rR")
    host_major = _parse_l4t_tuple(l4t_version)[0]
    tag_major = _parse_l4t_tuple(tag)[0]
    if tag == exact:
        logger.info("L4T_JETPACK_IMAGE auto-set to %s", L4T_JETPACK_IMAGE)
    elif tag_major != host_major:
        logger.warning(
            "L4T_JETPACK_IMAGE set to %s — host L4T %s has no NGC series "
            "(no r%d.x tags; JetPack 7 / r39 is unpublished). "
            "Container tests may fail until NVIDIA publishes a matching image.",
            L4T_JETPACK_IMAGE, l4t_version, host_major,
        )
    else:
        logger.info(
            "L4T_JETPACK_IMAGE set to %s (host L4T %s is not on NGC; "
            "using newest published tag <= host, typically r36.4.0 on RHEL 9.8)",
            L4T_JETPACK_IMAGE, l4t_version,
        )

# Default building container image timeout in seconds (30 minutes)
DEFAULT_BUILD_TIMEOUT = 900
# Default running container timeout in seconds (10 minutes)
DEFAULT_RUN_TIMEOUT = 600

def build_container_image(ssh, dockerfile_path, image_tag, context_files=None,
                          build_args=None, timeout=DEFAULT_BUILD_TIMEOUT, suite_name="test"):
    """
    Build a container image from a Dockerfile on the remote device.

    Skips build if image with the same tag already exists on the device.
    The Dockerfile should only COMPILE/INSTALL — NOT run tests.

    Args:
        ssh: SSHConnection instance
        dockerfile_path: Local Path to Dockerfile to upload
        image_tag: Tag for the built image (e.g., "l4t-cuda-tests:r36.4.0-v12.9")
        context_files: Optional list of local Path objects to upload alongside Dockerfile
        build_args: Optional dict of build args (e.g., {"CUDA_SAMPLES_VERSION": "v12.9"})
        timeout: Build timeout in seconds (default is value in seconds of DEFAULT_BUILD_TIMEOUT)
        suite_name: Name prefix for temp dir (e.g., "cuda", "vpi") for easy identification

    Returns:
        image_tag (str) — the tag of the built (or existing) image
    """
    check = ssh.sudo(
        podman_command(f"image exists {image_tag}"), fail_on_rc=False
    )
    if check.exit_status == 0:
        logger.info("Image %s already exists, skipping build", image_tag)
        return image_tag

    tmp = ssh.run(f"mktemp -d /tmp/test-{suite_name}-XXXXXX").stdout.strip()
    ssh.put(dockerfile_path, f"{tmp}/Dockerfile")
    if context_files:
        for f in context_files:
            ssh.put(f, f"{tmp}/{f.name}")

    all_args = dict(build_args or {})
    if "L4T_JETPACK_IMAGE" not in all_args:
        all_args["L4T_JETPACK_IMAGE"] = get_l4t_jetpack_image()
    if "CACHEBUST" not in all_args:
        all_args["CACHEBUST"] = str(datetime.now().timestamp())
    args_str = " ".join(f"--build-arg {k}='{v}'" for k, v in all_args.items())

    cmd = f"{podman_command()} build -t {image_tag} {PODMAN_GPU_FLAGS} {args_str} {tmp}"
    try:
        ssh.sudo(cmd, timeout=timeout)
        return image_tag
    finally:
        # A failed build can leave large untagged layers behind.  Prune only
        # dangling images; keep named base images cached for later tests.
        ssh.sudo(
            f"{podman_command()} image prune -f", fail_on_rc=False,
            print_output=False,
        )


def run_container(ssh, image_tag, command="", timeout=DEFAULT_RUN_TIMEOUT, extra_flags=""):
    """
    Run a command in a container image via podman run --rm.
    Applies RHEL-specific GPU flags automatically.

    Args:
        ssh: SSHConnection instance
        image_tag: Image tag to run
        command: Command to execute inside the container
        timeout: Run timeout in seconds
        extra_flags: Additional podman run flags

    Returns:
        Result object with stdout, stderr, exit_status
    """
    cmd = f"{podman_command()} run --rm {PODMAN_GPU_FLAGS} {extra_flags} {image_tag} {command}"
    return ssh.sudo(cmd, timeout=timeout, fail_on_rc=False)


def cleanup_container_image(ssh, image_tag):
    """Remove a built image to free disk space."""
    ssh.sudo(
        f"{podman_command()} rmi -f {image_tag}", fail_on_rc=False
    )
    ssh.sudo(
        f"{podman_command()} image prune -f", fail_on_rc=False,
        print_output=False,
    )
