"""
Kernel module (kmod) tests for Jetson RPMs.
Covers nvidia-jetpack-kmod: device nodes and loaded NVIDIA kernel modules.
Based on orin-kmods coverage from nvidia-jetson-sidecar.
"""


class TestKmod:
    """Test NVIDIA kernel driver and device nodes on Jetson devices."""

    def test_nvidia_devices(self, ssh):
        """Test NVIDIA device nodes are present (/dev/nv*)."""
        result = ssh.sudo("ls -la /dev/nv*")
        assert len(result.stdout.splitlines()) > 0, "No NVIDIA devices found"

    def test_tegra_devices(self, ssh):
        """Test Tegra device nodes are present (/dev/tegra*)."""
        result = ssh.sudo("ls -la /dev/tegra*", fail_on_rc=False)
        assert result.exit_status == 0, f"Failed to list Tegra devices: {result.stderr}"

    def test_nvidia_kernel_modules(self, ssh):
        """Test NVIDIA kernel modules are loaded."""
        result = ssh.sudo("lsmod | grep nvidia")
        assert "nvidia" in result.stdout, "No NVIDIA kernel modules loaded"
