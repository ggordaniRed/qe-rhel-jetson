"""
PCI tests for Jetson RPMs.
Based on test_basic.py and test_basic_locally.py from edge-ai-image-pipelines.
"""
import re
import pytest
from tests_suites import conftest as _conftest
from logging import getLogger
logger = getLogger(__name__)

def parse_lnkcap_lines(lspci_output: str) -> list[dict]:
    """
    Parse LnkCap lines from lspci -vv output.
    Returns list of dicts with 'speed' (e.g. '16GT/s') and 'width' (e.g. 8).
    """
    results = []
    for line in lspci_output.splitlines():
        speed_match = re.search(r'Speed (\d+(?:\.\d+)?GT/s)', line)
        width_match = re.search(r'Width x(\d+)', line)
        if speed_match and width_match:
            results.append({
                'speed': speed_match.group(1),
                'width': int(width_match.group(1)),
                'raw': line.strip()
            })
    return results


def speed_to_gen(speed: str) -> int:
    """Convert PCIe speed string to generation number."""
    speed_map = {
        '2.5GT/s': 1,
        '5GT/s': 2,
        '8GT/s': 3,
        '16GT/s': 4,
        '32GT/s': 5,
        '64GT/s': 6,
    }
    return speed_map.get(speed, 0)


class TestPCIs:
    """Test PCI functionality on Jetson devices."""

    def test_pcie(self, ssh):
        """Test PCI devices are present."""
        result = ssh.sudo("ls -1 /sys/bus/pci/devices/", fail_on_rc=False)
        assert result.exit_status == 0, f"Failed to list PCI devices: {result.stderr}"
        assert len(result.stdout.splitlines()) > 0, "No PCI devices found"

    def test_pci_gen_capability(self, ssh):
        """
        Test PCIe generation capability matches hardware spec.

        Validates that:
        1. At least one PCIe link is present
        2. At least one link supports the expected PCIe generation speed

        This test is flexible - it doesn't require specific slots to be populated,
        only that the hardware demonstrates its expected PCIe generation capability.
        """
        spec = _conftest.get_hardware_spec(_conftest.HARDWARE_MODEL_NAME)
        pci_spec = spec.get("pcis", {})

        if not pci_spec:
            pytest.skip("No PCIe spec defined for this hardware")

        ssh.sudo("dnf install pciutils -y")

        result = ssh.sudo("lspci -vv | grep -P 'LnkCap:'", fail_on_rc=False)
        assert result.exit_status == 0, "No PCIe LnkCap information found. Is lspci working?"

        detected_links = parse_lnkcap_lines(result.stdout)
        assert len(detected_links) > 0, (
            f"No PCIe links detected.\n"
            f"Raw lspci output:\n{result.stdout}"
        )

        # Determine the expected max speed from spec (highest speed defined)
        expected_speeds = set()
        for controller_spec in pci_spec.values():
            if isinstance(controller_spec, dict) and 'capable_speed' in controller_spec:
                expected_speeds.add(controller_spec['capable_speed'])

        if not expected_speeds:
            pytest.skip("No capable_speed defined in PCIe spec")

        max_expected_speed = max(expected_speeds, key=speed_to_gen)
        max_expected_gen = speed_to_gen(max_expected_speed)

        # Check that at least one link supports the expected generation
        detected_speeds = [link['speed'] for link in detected_links]
        detected_gens = [speed_to_gen(s) for s in detected_speeds]
        max_detected_gen = max(detected_gens) if detected_gens else 0

        # Format detected links for diagnostics
        detected_summary = "\n".join(
            f"  - {link['speed']}, Width x{link['width']}"
            for link in detected_links
        )

        assert max_detected_gen >= max_expected_gen, (
            f"PCIe generation capability mismatch.\n"
            f"Expected: At least Gen{max_expected_gen} ({max_expected_speed})\n"
            f"Detected max: Gen{max_detected_gen}\n\n"
            f"Detected PCIe links:\n{detected_summary}"
        )

        # Log what was found for informational purposes
        logger.info("\nPCIe capability check passed:")
        logger.info(f"  Expected generation: Gen{max_expected_gen} ({max_expected_speed})")
        logger.info(f"  Detected {len(detected_links)} link(s):")
        logger.info(detected_summary)
