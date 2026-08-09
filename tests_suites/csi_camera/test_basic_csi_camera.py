"""
CSI camera tests for Jetson RPMs.

Known Issues
============

1. MIPI/CSI2 Camera Modules Blacklisted (RHEL 9)
   Issue: Camera kernel modules (tegra_camera, tegra_camera_platform, tegra_camera_rtcpu)
          are blacklisted in kmod RPM's nvidia-camera.conf. The camera RPM is
          ready, but MIPI/CSI2 requires kernel patches not available in RHEL 9.
   References: RHEL-56474, RHELBU-2601
   Developer: Nirmala Dalvi — investigating whether RHEL 9.8 has the patches,
              testing Allied Vision MIPI cameras on 9.7/9.8 nightly.
   Contact: hgeaydem — confirmed MIPI/CSI2 is NOT a GA feature for RHEL 9.
            Most customers use USB/network cameras (no kmods needed).
   Affected tests: test_csi_camera_devices, test_csi_camera_sysfs,
                    test_camera_driver_loaded
   Fix applied: assert -> warnings.warn() so tests pass on both RPM-only
                and bootc setups.
   TODO: If RHEL 9.7/9.8+ gets patches — unblacklist modules, convert back to asserts.
         For RHEL 10+ — convert to asserts (patches confirmed in RHEL-56474).
"""
import warnings


class TestCSICamera:
    """Test CSI camera functionality on Jetson devices."""

    def test_csi_camera_devices(self, ssh):
        """Test CSI camera device nodes are present.
        Checks for /dev/video* nodes created by the videodev module. """

        result = ssh.run("ls -la /dev/video*", fail_on_rc=False)
        # TODO: Convert to assert once MIPI/CSI2 camera support is confirmed for the target RHEL version
        #       (see Known Issue #1 above).
        if result.exit_status != 0:
            warnings.warn(UserWarning("No CSI camera (/dev/video*) devices found"))

    def test_csi_camera_sysfs(self, ssh):
        """Test CSI camera sysfs entries.
        Checks /sys/class/video4linux/ which is created by the videodev module,
        (This directory is only populated when camera drivers load successfully)."""

        result = ssh.run("ls -1 /sys/class/video4linux/", fail_on_rc=False)
        # TODO: Convert to assert once MIPI/CSI2 camera support is confirmed
        #       for the target RHEL version (see Known Issue #1 above).
        if result.exit_status != 0:
            warnings.warn(UserWarning(
                "No /sys/class/video4linux/ found (camera kernel modules are blacklisted in nvidia-camera.conf)"
            ))

    def test_camera_driver_loaded(self, ssh):
        """Test camera driver is loaded.
        Checks if camera kernel modules
        (tegra_camera, tegra_camera_platform, tegra_camera_rtcpu) are loaded."""

        result = ssh.run("lsmod | grep camera", fail_on_rc=False)
        # TODO: Convert to assert once MIPI/CSI2 camera support is confirmed
        #       for the target RHEL version (see Known Issue #1 above).
        if result.exit_status != 0:
            warnings.warn(UserWarning(
                "No camera kernel modules loaded — blacklisted in nvidia-camera.conf "
            ))
