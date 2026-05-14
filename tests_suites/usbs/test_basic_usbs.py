"""
USB tests for Jetson RPMs.
Based on test_basic.py and test_basic_locally.py from edge-ai-image-pipelines.
"""

# 480M = USB2.0 / For charging only
# 5000M/ 10000M = USB3.2 Gen1/ USB3.2 Gen2
# hub/4p = 4x Ports

import re
from tests_suites import conftest as _conftest


class TestUSBs:
    """Test USB functionality on Jetson devices."""

    def test_usb(self, ssh):
        """Test USB devices are present."""
        result = ssh.sudo("ls -1 /sys/bus/usb/devices/", fail_on_rc=False)
        assert result.exit_status == 0, f"Failed to list USB devices: {result.stderr}"
        assert len(result.stdout.splitlines()) > 0, "No USB devices found"

    def test_usb_spec(self, ssh):
        """
        Test USB specification against lsusb output.

        For each unique speed in the spec:
        1. Get actual lsusb lines matching that speed
        2. Parse actual port counts and group by port count
        3. Pair actual groups with spec groups (sorted by port count)
        4. Verify actual ports >= expected ports and group sizes match

        This ensures that if the spec defines N controllers with the same
        (ports, speed), the actual output also shows N controllers with
        a consistent port count — which may be higher than spec but must
        match among themselves.
        """
        spec = _conftest.get_hardware_spec(_conftest.HARDWARE_MODEL_NAME)
        ssh.sudo("dnf install usbutils -y")  # for lsusb cli tool
        usbs = spec.get("usbs")

        # Group spec by (ports, speed): count controllers with identical config
        spec_groups = {}
        for _, values in usbs.items():
            key = (values.get("ports"), values.get("capable_speed"))
            spec_groups[key] = spec_groups.get(key, 0) + 1

        # Process each unique speed
        unique_speeds = set(speed for (_, speed) in spec_groups)

        for speed in unique_speeds:
            # Expected groups for this speed: [(ports, count), ...] sorted by ports
            expected = sorted(
                [(ports, count) for (ports, s), count in spec_groups.items() if s == speed]
            )

            # Get actual lsusb lines for this speed
            result = ssh.sudo(f"lsusb -t | grep -v Bus | grep hub | grep {speed}", fail_on_rc=False)
            assert result.exit_status == 0, f"Failed to find USB with speed {speed}: {result.stderr}"
            lines = [ln for ln in result.stdout.splitlines() if ln.strip()]

            # Parse actual port counts from each line
            actual_port_counts = []
            for line in lines:
                port_match = re.search(r"/(\d+)p", line)
                assert port_match, f"Could not parse port count from lsusb line: {line}"
                actual_port_counts.append(int(port_match.group(1)))

            # Group actual by port count: [(ports, count), ...] sorted by ports
            actual_port_groups = {}
            for p in actual_port_counts:
                actual_port_groups[p] = actual_port_groups.get(p, 0) + 1
            actual = sorted(actual_port_groups.items())

            # Must have same number of port groups
            assert len(actual) == len(expected), (
                f"USB @ {speed}: expected {len(expected)} port group(s) {expected}, "
                f"got {len(actual)} actual group(s) {actual}"
            )

            # Pair sorted groups: actual ports >= expected ports, counts must match
            for (exp_ports, exp_count), (act_ports, act_count) in zip(expected, actual):
                assert act_ports >= exp_ports, (
                    f"USB @ {speed}: actual {act_ports} port(s) < expected {exp_ports}"
                )
                assert act_count == exp_count, (
                    f"USB @ {speed}: expected {exp_count} controller(s) with >={exp_ports}p, "
                    f"got {act_count} with {act_ports}p"
                )
