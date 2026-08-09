"""
PVA (Programmable Vision Accelerator) / VPI tests for Jetson RPMs.
- Hardware check: verifies PVA device presence matches hardware spec.
- VPI samples: 19 samples across all backends (cpu, cuda, pva, vic, ofa, ofa-pva-vic)
  via L4T container with CDI device exposure + PVA auth disable.
"""
import pytest
import os
import time
import logging
from pathlib import Path
from tests_suites import conftest as _conftest
from tests_resources.container_ops import (
    build_container_image, run_container, cleanup_container_image,
    L4T_JETPACK_IMAGE,
)

logger = logging.getLogger(__name__)

FILE = Path(os.path.realpath(__file__)).parent


class TestPVA:
    """Test PVA/VPI functionality on Jetson devices."""

    MAX_RETRIES = 2

    @pytest.fixture(scope="class")
    def l4t_vpi_image(self, ssh):
        """Build L4T VPI image once per class, clean up after."""
        tag = f"l4t-vpi-tests:{L4T_JETPACK_IMAGE.split(':')[1]}"
        build_container_image(
            ssh, FILE / "Dockerfile.l4t_vpi", tag,
            context_files=[FILE / "run-vpi-tests.sh"],
            suite_name="vpi",
        )
        yield tag
        # Teardown
        cleanup_container_image(ssh, tag)

    @pytest.mark.critical
    def test_pva_hardware_check(self, ssh):
        """Verify PVA hardware is present and accessible on this device."""
        spec = _conftest.get_hardware_spec(_conftest.HARDWARE_MODEL_NAME)
        result = ssh.sudo("ls -1 /sys/kernel/debug/pva0", fail_on_rc=False)
        if not spec.get("pva", {}).get("supported"):
            assert result.exit_status != 0, (
                "/sys/kernel/debug/pva found, but not supported on this hardware "
                "(see jetson_hardware_specs.yaml)"
            )
        else:
            assert result.exit_status == 0, (
                f"PVA devices not loaded on the system: {result.stderr}"
            )

    @pytest.mark.critical
    def test_vpi_all_samples(self, ssh, l4t_vpi_image):
        """Test all VPI samples across all backends via L4T container.
        Uses CDI device exposure + PVA auth disable.
        Retries up to MAX_RETRIES times — dcf_tracker CUDA backend is flaky due to GPU address-space fragmentation
        (VPI 3.x bug,fixed in VPI 4.0.3: https://docs.nvidia.com/vpi/release_notes_4_0_5.html)."""
        spec = _conftest.get_hardware_spec(_conftest.HARDWARE_MODEL_NAME)
        # Disable PVA auth if PVA supported (needed for pva and ofa-pva-vic backends)
        if spec.get("pva", {}).get("supported"):
            result = ssh.sudo("ls -1 /sys/kernel/debug/pva0", fail_on_rc=False)
            if result.exit_status == 0:
                ssh.sudo("bash -c 'echo 0 > /sys/kernel/debug/pva0/vpu_app_authentication'")

        # Skip backends not available on this hardware (e.g., Nano has no PVA/OFA)
        skip_backends = []
        extra_flags = ""
        if not spec.get("pva", {}).get("supported"):
            skip_backends.extend(["pva", "ofa", "ofa-pva-vic"])
            extra_flags = f"-e SKIP_BACKENDS={','.join(skip_backends)}"

        last_result = None
        for attempt in range(1, self.MAX_RETRIES + 2):
            if attempt > 1:
                logger.warning(
                    "VPI test attempt %d/%d — retrying after failure "
                    "(dcf_tracker CUDA flaky due to GPU address-space fragmentation)",
                    attempt, self.MAX_RETRIES + 1,
                )
                ssh.sudo("sync; sync; sync", fail_on_rc=False)
                ssh.sudo("echo 3 | tee /proc/sys/vm/drop_caches", fail_on_rc=False)
                time.sleep(5)

            last_result = run_container(ssh, l4t_vpi_image, "/opt/run-vpi-tests.sh", timeout=600, extra_flags=extra_flags)
            if last_result.exit_status == 0:
                if attempt > 1:
                    logger.info("VPI test passed on attempt %d/%d", attempt, self.MAX_RETRIES + 1)
                break
        else:
            logger.error("VPI test failed after %d attempts", self.MAX_RETRIES + 1)

        assert last_result.exit_status == 0, (
            f"VPI tests failed after {self.MAX_RETRIES + 1} attempts "
            f"(see output above for details): {last_result.stderr}"
        )
