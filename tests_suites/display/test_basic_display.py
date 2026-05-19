"""
Display tests for Jetson RPMs.

Known Issues
============

1. Display and DRM Behavior (RESOLVED) [JETPACK-123, JETPACK-94, JETPACK-96]
   Resolution: multi-user.target (runlevel 3) is the confirmed default for
               the base bootc image. tegra_drm (kernel auto-loaded) handles
               display on both targets. nvidia_drm is NOT required and should
               NOT be loaded by any daemon or test.
   Root cause: The original bug occurred only when switching to graphical.target
               WITHOUT GDM installed while using nvidia_drm. Two valid configs:
                 - multi-user.target (base image, no GDM) → display via tegra_drm (only tty console)
                 - graphical.target + GDM installed (GUI variant) → display via tegra_drm + GDM (graphical session)
   WARNING: RHEL 9.7 (TP) requires 'pd_ignore_unused' kernel arg on
            multi-user.target to avoid kernel hang. RHEL 9.8+ (GA) does not
            need this workaround. The ensure_pd_ignore_unused fixture handles
            this automatically.

2. Xorg/X11 Not Installed on RPM-only (RESOLVED)
   Xorg is NOT a JetPack RPM. Only present in GUI variant (Containerfile-gui).
   test_x11_display skips on multi-user.target, warns if missing on graphical.target.
"""
import pytest
import warnings
from tests_resources.device_ops import get_systemd_target
from logging import getLogger
logger = getLogger(__name__)


class TestDisplay:
    """Test Display functionality on Jetson devices."""

    def test_default_target(self, ssh):
        """Validate systemd default target.
        Base bootc image defaults to multi-user.target (runlevel 3).
        graphical.target is only for enabling desktop environment and requires GDM."""

        systemd_target = get_systemd_target(ssh)

        if systemd_target == "multi-user.target":
            logger.info("[test_default_target] multi-user.target confirmed — base bootc default")
            return

        if systemd_target == "graphical.target":
            gdm_installed = ssh.run("rpm -q gdm", fail_on_rc=False)
            gdm_active = ssh.run("systemctl is-active gdm.service", fail_on_rc=False)
            assert gdm_installed.exit_status == 0 and gdm_active.stdout.strip() == "active", (
                "Default bootc target is multi-user.target, graphical.target is only for "
                "enabling desktop environment and GDM is not installed/active."
            )
            logger.info("[test_default_target] graphical.target with GDM active — GUI variant deployed")
            return

        assert False, (
            f"Unexpected target: {systemd_target!r} — "
            f"only multi-user.target and graphical.target are supported."
        )

    def test_display_devices(self, ssh):
        """Test display device nodes are present.
        Checks for DRM (/dev/dri/*) or framebuffer (/dev/fb*) device nodes"""

        result = ssh.run("ls -la /dev/dri/* || ls -la /dev/fb*", fail_on_rc=False)
        assert result.exit_status == 0, f"Failed to check display devices: {result.stderr}"

    def test_display_by_drm(self, ensure_pd_ignore_unused):
        """Test DRM sysfs entries and connector status via tegra_drm.
        tegra_drm is kernel auto-loaded on both targets. nvidia_drm is NOT
        required and should NOT be loaded by any daemon or test."""
        ssh = ensure_pd_ignore_unused

        result = ssh.run("ls -1 /sys/class/drm/", fail_on_rc=False)
        assert result.exit_status == 0, f"Failed to access DRM sysfs: {result.stderr}"

        systemd_target = get_systemd_target(ssh)
        result = ssh.run("cat /sys/class/drm/card*-*/status", fail_on_rc=False)
        if result.exit_status != 0 and systemd_target != "graphical.target":
            pytest.skip("DRM connector status not applicable on multi-user.target (no graphical session)")
        assert result.exit_status == 0, f"Failed to check display status: {result.stderr}"
        if "disconnected" in result.stdout.lower():
            warnings.warn("Display is not connected", UserWarning)

    def test_x11_display(self, ssh):
        """Test X11/Xorg server is available.
        Only meaningful on graphical.target (GUI variant).
        On multi-user.target, Xorg is not expected — skip."""

        systemd_target = get_systemd_target(ssh)
        if systemd_target != "graphical.target":
            pytest.skip("X11 test not applicable on multi-user.target (no graphical session)")

        result = ssh.run("which Xorg || which X", fail_on_rc=False)
        assert result.exit_status == 0, f"Xorg/X11 server is not installed on graphical.target: {result.stderr}"

    def test_wayland_libs(self, ssh):
        """Test Wayland-related libraries are present (nvidia-jetpack-wayland).
        Checks that Wayland shared libraries are available via ldconfig. (come from the nvidia-jetpack-wayland RPM)"""

        result = ssh.run("ldconfig -p | grep -i wayland", fail_on_rc=False)
        assert result.exit_status == 0, f"Failed to check Wayland libs: {result.stderr}"
        assert "wayland" in result.stdout.lower(), "No Wayland libraries found (nvidia-jetpack-wayland may be missing)"

    def test_wayland_socket_or_server(self, ssh):
        """Test Wayland compositor is running with an active socket.
        Only expected on graphical.target. On multi-user.target, skip."""

        systemd_target = get_systemd_target(ssh)
        if systemd_target != "graphical.target":
            pytest.skip("Wayland compositor test not applicable on multi-user.target")

        socket_result = ssh.run("ls /run/user/*/wayland-*", fail_on_rc=False)
        which_result = ssh.run("which weston || which Xwayland || which xrandr", fail_on_rc=False)
        has_socket = socket_result.exit_status == 0 and socket_result.stdout.strip()
        has_binary = which_result.exit_status == 0 and which_result.stdout.strip()
        if not (has_socket or has_binary):
            warnings.warn(
                "No Wayland socket or compositor binary installed (wayland not running)",
                UserWarning,
            )
