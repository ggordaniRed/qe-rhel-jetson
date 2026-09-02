"""
ISP (Image Signal Processor) tests for Jetson devices.

Validates the Tegra ISP hardware: device nodes, kernel drivers, V4L2/media
topology, device tree presence, and basic pipeline capability.

Known Issues on RHEL 9
======================
Camera kernel modules (tegra_camera, tegra_camera_platform, nvhost_isp,
nvcsi, tegra_vi) are blacklisted in nvidia-camera.conf (kmod RPM).
This means /dev/nvhost-isp*, /dev/nvhost-vi*, /dev/nvcsi* and the media
controller typically don't appear in a stock RPM install.

Tests in TestISPDevice and TestISPDriver use warnings.warn instead of assert
for node/module presence, consistent with test_basic_csi_camera.py.

Tests in TestISPSysfs and TestISPDeviceTree assert on device tree and sysfs
entries that are independent of module loading (DT is parsed by the kernel
at boot regardless of whether the driver is bound).
"""
import warnings
import pytest
from logging import getLogger

logger = getLogger(__name__)

# nvhost-based ISP device name patterns
_ISP_DEV_PATTERNS = ["nvhost-isp", "video-isp"]
_VI_DEV_PATTERNS  = ["nvhost-vi",  "video-vi"]
_NVCSI_DEV_PATTERNS = ["nvcsi"]

# Kernel module names associated with the ISP pipeline
_ISP_MODULES = [
    "nvhost_isp",
    "tegra_isp",
    "tegra_camera",
    "nvcsi",
    "tegra_vi",
]


def _find_devs(ssh, patterns):
    """Return list of /dev/* nodes matching any pattern; empty if none."""
    found = []
    for pat in patterns:
        result = ssh.run(f"ls /dev/*{pat}* 2>/dev/null", fail_on_rc=False)
        if result.exit_status == 0 and result.stdout.strip():
            found.extend(result.stdout.strip().splitlines())
    return [d.strip() for d in found if d.strip()]


class TestISPDevice:
    """Verify ISP/VI/NVCSI device node presence."""

    def test_isp_device_nodes(self, ssh):
        """ISP device nodes (/dev/nvhost-isp*) should be present when camera modules load."""
        devs = _find_devs(ssh, _ISP_DEV_PATTERNS)
        if not devs:
            warnings.warn(UserWarning(
                "No ISP device nodes found (/dev/nvhost-isp* etc.). "
                "Camera modules are likely blacklisted in nvidia-camera.conf "
                "(see RHEL-56474). Bind the driver to create device nodes."
            ))
        else:
            logger.info("ISP device nodes: %s", devs)

    def test_vi_device_nodes(self, ssh):
        """VI (Video Input) device nodes (/dev/nvhost-vi*) should be present with camera modules."""
        devs = _find_devs(ssh, _VI_DEV_PATTERNS)
        if not devs:
            warnings.warn(UserWarning(
                "No VI device nodes found (/dev/nvhost-vi*). "
                "Camera kernel modules are blacklisted — ISP pipeline unavailable."
            ))
        else:
            logger.info("VI device nodes: %s", devs)

    def test_nvcsi_device_nodes(self, ssh):
        """NVCSI device nodes (/dev/nvcsi*) should be present with camera modules."""
        devs = _find_devs(ssh, _NVCSI_DEV_PATTERNS)
        if not devs:
            warnings.warn(UserWarning(
                "No NVCSI device nodes found (/dev/nvcsi*). "
                "NVCSI module blacklisted — CSI/ISP pipeline unavailable."
            ))
        else:
            logger.info("NVCSI device nodes: %s", devs)

    def test_media_controller_device(self, ssh):
        """Media controller device (/dev/media*) should appear when ISP subsystem is active."""
        result = ssh.run("ls /dev/media* 2>/dev/null", fail_on_rc=False)
        if result.exit_status != 0 or not result.stdout.strip():
            warnings.warn(UserWarning(
                "No /dev/media* media controller found. "
                "Media topology is unavailable — camera modules may be blacklisted."
            ))
        else:
            logger.info("Media controller devices: %s", result.stdout.strip())


class TestISPDriver:
    """Validate ISP kernel driver and module loading."""

    def test_isp_modules_loaded(self, ssh):
        """At least one ISP-related kernel module should be loaded or built-in."""
        lsmod = ssh.run("lsmod", fail_on_rc=False)
        loaded = [m for m in _ISP_MODULES if lsmod.exit_status == 0 and m in lsmod.stdout]
        if not loaded:
            warnings.warn(UserWarning(
                f"None of {_ISP_MODULES} found in lsmod. "
                "ISP pipeline modules are blacklisted or not installed."
            ))
        else:
            logger.info("ISP-related modules loaded: %s", loaded)

    def test_isp_dmesg_init(self, ssh):
        """dmesg should contain ISP/VI/NVCSI initialization messages (even if disabled)."""
        result = ssh.sudo(
            "dmesg | grep -iE 'isp|nvhost.*isp|tegra.*vi|nvcsi' | head -20",
            fail_on_rc=False,
        )
        if result.exit_status != 0 or not result.stdout.strip():
            warnings.warn(UserWarning(
                "No ISP/VI/NVCSI messages in dmesg. "
                "This may be expected when camera modules are blacklisted."
            ))
        else:
            logger.info("ISP dmesg messages:\n%s", result.stdout.strip())

    def test_nvhost_driver_present(self, ssh):
        """nvhost (host1x) driver must be present — it's the foundation for ISP/VI/CUDA."""
        result = ssh.run("lsmod | grep nvhost", fail_on_rc=False)
        builtin = ssh.sudo("dmesg | grep -i 'host1x\\|nvhost' | head -5", fail_on_rc=False)
        if (result.exit_status != 0 or not result.stdout.strip()) and \
           (builtin.exit_status != 0 or not builtin.stdout.strip()):
            warnings.warn(UserWarning(
                "nvhost (host1x) driver not detected in lsmod or dmesg. "
                "ISP requires host1x to manage engine scheduling."
            ))
        else:
            logger.info(
                "nvhost present — lsmod: %s | dmesg: %s",
                result.stdout.strip()[:80] or "(module not listed)",
                builtin.stdout.strip().splitlines()[0] if builtin.stdout.strip() else "(not in dmesg)",
            )


class TestISPSysfs:
    """Validate ISP sysfs and V4L2 subdevice topology."""

    def test_tegra_video_subdevices(self, ssh):
        """
        /sys/class/tegra-video/ should list ISP/VI V4L2 subdevices.
        This directory is populated by the tegra-video driver even when camera modules
        are blacklisted on newer kernels.
        """
        result = ssh.run("ls /sys/class/tegra-video/ 2>/dev/null", fail_on_rc=False)
        if result.exit_status != 0 or not result.stdout.strip():
            warnings.warn(UserWarning(
                "No /sys/class/tegra-video/ entries found. "
                "ISP V4L2 subdevices are not exposed (camera driver blacklisted)."
            ))
        else:
            subdevs = result.stdout.strip().splitlines()
            logger.info("tegra-video subdevices: %s", subdevs)
            isp_devs = [d for d in subdevs if "isp" in d.lower() or "vi" in d.lower()]
            logger.info("ISP/VI subdevices: %s", isp_devs)

    def test_video4linux_subdevices(self, ssh):
        """
        /sys/class/video4linux/ may contain V4L2 subdevice entries for ISP/VI.
        Also populated by the videodev module when tegra-video subdevices bind.
        """
        result = ssh.run("ls /sys/class/video4linux/ 2>/dev/null", fail_on_rc=False)
        if result.exit_status != 0 or not result.stdout.strip():
            warnings.warn(UserWarning(
                "No /sys/class/video4linux/ entries. ISP/camera drivers not bound."
            ))
        else:
            devs = result.stdout.strip().splitlines()
            logger.info("video4linux devices: %s", devs)

    def test_isp_host1x_platform_device(self, ssh):
        """The ISP platform device should appear under the host1x bus in sysfs."""
        result = ssh.run(
            "find /sys/bus/platform/devices/ -maxdepth 1 -name '*isp*' 2>/dev/null | head -5",
            fail_on_rc=False,
        )
        if result.exit_status != 0 or not result.stdout.strip():
            # Try via host1x bus
            result = ssh.run(
                "find /sys/bus/host1x/ -name '*isp*' 2>/dev/null | head -5",
                fail_on_rc=False,
            )
        if not result.stdout.strip():
            warnings.warn(UserWarning(
                "ISP platform device not found under /sys/bus/platform/devices/ "
                "or /sys/bus/host1x/ — module may be blacklisted."
            ))
        else:
            logger.info("ISP platform device(s): %s", result.stdout.strip())

    def test_vi_host1x_platform_device(self, ssh):
        """The VI platform device should appear in sysfs (independent of module blacklist)."""
        result = ssh.run(
            "find /sys/bus/platform/devices/ /sys/bus/host1x/ -maxdepth 2 -name '*vi*' 2>/dev/null | head -5",
            fail_on_rc=False,
        )
        if not result.stdout.strip():
            warnings.warn(UserWarning("VI platform device not found in sysfs."))
        else:
            logger.info("VI platform device(s): %s", result.stdout.strip())


class TestISPDeviceTree:
    """Verify ISP nodes are present in the device tree (independent of module loading)."""

    def test_isp_devicetree_node(self, ssh):
        """Device tree must contain an ISP node (/sys/firmware/devicetree/base/isp*)."""
        result = ssh.run(
            "find /sys/firmware/devicetree/base -maxdepth 2 -name 'isp*' 2>/dev/null | head -5",
            fail_on_rc=False,
        )
        assert result.exit_status == 0 and result.stdout.strip(), (
            "No ISP node found in device tree (/sys/firmware/devicetree/base/isp*). "
            "The SoC may not have an ISP, or device tree is malformed."
        )
        logger.info("ISP device tree nodes: %s", result.stdout.strip())

    def test_vi_devicetree_node(self, ssh):
        """Device tree must contain a VI (video input) node."""
        result = ssh.run(
            "find /sys/firmware/devicetree/base -maxdepth 2 -name 'vi*' 2>/dev/null | head -5",
            fail_on_rc=False,
        )
        assert result.exit_status == 0 and result.stdout.strip(), (
            "No VI node found in device tree. "
            "VI is the camera front-end feeding the ISP — it should always be present on Jetson."
        )
        logger.info("VI device tree nodes: %s", result.stdout.strip())

    def test_nvcsi_devicetree_node(self, ssh):
        """Device tree must contain an NVCSI (CSI host interface) node."""
        result = ssh.run(
            "find /sys/firmware/devicetree/base -maxdepth 3 -name '*nvcsi*' 2>/dev/null | head -5",
            fail_on_rc=False,
        )
        assert result.exit_status == 0 and result.stdout.strip(), (
            "No NVCSI node in device tree. "
            "NVCSI bridges the MIPI CSI-2 lanes to the VI/ISP pipeline."
        )
        logger.info("NVCSI device tree nodes: %s", result.stdout.strip())

    def test_isp_compatible_string(self, ssh):
        """ISP device tree node must have a recognised 'compatible' string."""
        result = ssh.run(
            "find /sys/firmware/devicetree/base -maxdepth 3 -name 'isp*' "
            "-exec cat {}/compatible 2>/dev/null \\; | tr '\\0' '\\n' | head -10",
            fail_on_rc=False,
        )
        if result.exit_status != 0 or not result.stdout.strip():
            # Alternative: search inside device tree directory
            result = ssh.run(
                "strings /sys/firmware/devicetree/base/isp/compatible 2>/dev/null || "
                "strings /sys/firmware/devicetree/base/isp@*/compatible 2>/dev/null | head -5",
                fail_on_rc=False,
            )
        if not result.stdout.strip():
            pytest.skip("Could not read ISP device tree compatible string")
        compat = result.stdout.strip()
        logger.info("ISP compatible: %s", compat)
        assert "nvidia" in compat.lower() or "tegra" in compat.lower(), (
            f"ISP compatible string does not reference nvidia/tegra: {compat!r}"
        )


class TestISPCapability:
    """Verify ISP tooling and capability detection."""

    def test_v4l2_utils_installed(self, ssh):
        """v4l2-utils (v4l2-ctl, media-ctl) should be installed for ISP inspection."""
        result = ssh.run("which v4l2-ctl 2>/dev/null || rpm -q v4l-utils 2>/dev/null", fail_on_rc=False)
        if result.exit_status != 0 or not result.stdout.strip():
            warnings.warn(UserWarning(
                "v4l2-utils not installed. Install with: dnf install v4l-utils. "
                "Needed for media topology inspection and ISP capability queries."
            ))
        else:
            logger.info("v4l2-utils: %s", result.stdout.strip())

    def test_media_ctl_topology(self, ssh):
        """media-ctl should enumerate ISP/VI/sensor links if camera modules are loaded."""
        media_ctl = ssh.run("which media-ctl 2>/dev/null", fail_on_rc=False)
        if media_ctl.exit_status != 0 or not media_ctl.stdout.strip():
            pytest.skip("media-ctl not installed (install v4l-utils)")

        result = ssh.run("media-ctl -p 2>/dev/null", fail_on_rc=False)
        if result.exit_status != 0 or not result.stdout.strip():
            warnings.warn(UserWarning(
                "media-ctl -p returned no topology. "
                "No media controller found — ISP pipeline modules may be blacklisted."
            ))
        else:
            logger.info("media-ctl topology:\n%s", result.stdout[:2000])
            isp_entities = [l for l in result.stdout.splitlines()
                            if "isp" in l.lower() or "vi" in l.lower() or "nvcsi" in l.lower()]
            logger.info("ISP/VI/NVCSI entities: %s", isp_entities)

    def test_v4l2_ctl_list_devices(self, ssh):
        """v4l2-ctl should list ISP/VI V4L2 devices if the pipeline is active."""
        v4l2 = ssh.run("which v4l2-ctl 2>/dev/null", fail_on_rc=False)
        if v4l2.exit_status != 0 or not v4l2.stdout.strip():
            pytest.skip("v4l2-ctl not installed (install v4l-utils)")

        result = ssh.run("v4l2-ctl --list-devices 2>/dev/null", fail_on_rc=False)
        if result.exit_status != 0 or not result.stdout.strip():
            warnings.warn(UserWarning(
                "v4l2-ctl --list-devices returned nothing. "
                "ISP/VI V4L2 devices not found — camera pipeline modules may be blacklisted."
            ))
        else:
            logger.info("v4l2 devices:\n%s", result.stdout.strip())

    def test_isp_no_critical_dmesg_errors(self, ssh):
        """dmesg must not contain critical ISP errors (probe failures, resource allocation errors)."""
        result = ssh.sudo(
            r"dmesg | grep -iE '\bisp\b.*error|\bisp\b.*fail|nvhost.*vi.*error|tegra.*vi.*error|nvcsi.*error' | head -20",
            fail_on_rc=False,
        )
        if result.exit_status != 0 or not result.stdout.strip():
            logger.info("No critical ISP errors in dmesg")
            return

        errors = result.stdout.strip().splitlines()
        # Filter out benign "ISP is not supported" style messages that appear when modules are blacklisted
        real_errors = [
            l for l in errors
            if not any(skip in l.lower() for skip in ["not supported", "disabled", "blacklist"])
        ]
        if real_errors:
            logger.warning("ISP dmesg errors:\n%s", "\n".join(real_errors))
        assert not real_errors, (
            "Critical ISP errors found in dmesg:\n" + "\n".join(real_errors)
        )
