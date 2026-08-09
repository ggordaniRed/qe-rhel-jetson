"""
Tools tests for Jetson RPMs.
Covers nvidia-jetpack-tools: nvpmodel (power model) and nvfancontrol.
"""
import re
import pytest
from tests_suites import conftest as _conftest
from logging import getLogger
logger = getLogger(__name__)


def _power_modes_spec(spec):
    """Return (power_modes_value, kind) where kind is 'list' or 'range'."""
    power_modes = spec.get("tools").get("power_modes")
    if power_modes is None:
        raise ValueError("power_modes is not set in jetson_hardware_specs")
    if isinstance(power_modes, dict) and "min" in power_modes and "max" in power_modes:
        return power_modes, "range"
    if isinstance(power_modes, (list, tuple)):
        return power_modes, "list"
    return power_modes, None


def _parse_wattage_from_stdout(stdout):
    """Extract wattage values (e.g. 15, 30, 60) from nvpmodel output. Returns list of ints."""
    # Match numbers followed by 'W' (e.g. "15W", "60 W", "MODE_30W", "NV Power Mode: MODE_30W")
    matches = re.findall(r"(\d+)\s*W\b", stdout, re.IGNORECASE)
    logger.info(f"[test_basic_tools] _parse_wattage_from_stdout] matches: {matches}")
    return [int(m) for m in matches] if matches else []


class TestTools:
    """Test nvidia-jetpack-tools on Jetson devices."""

    def test_nvpmodel_query(self, ssh):
        """Test nvpmodel can report power model (nvidia-jetpack-tools)."""
        spec = _conftest.get_hardware_spec(_conftest.HARDWARE_MODEL_NAME)
        power_modes_val, kind = _power_modes_spec(spec)
        assert kind in ("list", "range"), (
            f"power_modes in jetson_hardware_specs must be a list (specific) or {{min, max}} (range); got {power_modes_val!r}"
        )
        result = ssh.run("nvpmodel -q", fail_on_rc=False)
        assert result.exit_status == 0, f"nvpmodel -q failed: {result.stderr}"
        assert result.stdout.strip(), "nvpmodel produced no output"
        # Check if the power model is a number in the second line of the output
        assert result.stdout.splitlines()[1].isdigit(), "nvpmodel -q produced no power model"
        # Check if the power model is in the allowed list or range
        # MAXN is always a valid power mode (maximum performance, no wattage cap)
        if "MAXN" in result.stdout:
            return
        if kind == "list":
            expected_modes = tuple(power_modes_val)
            assert any(m in result.stdout for m in expected_modes), (
                f"Expected one of power modes {expected_modes} or MAXN in nvpmodel output"
            )
        else:
            min_w, max_w = power_modes_val["min"], power_modes_val["max"]
            wattages = _parse_wattage_from_stdout(result.stdout)
            assert wattages, (
                "Could not find any wattage value (e.g. 15W, 60W) or MAXN in nvpmodel output for range check"
            )
            in_range = [w for w in wattages if min_w <= w <= max_w]
            assert in_range, (
                f"Power in nvpmodel output {wattages} W not in allowed range [{min_w}, {max_w}] W"
            )

    def test_nvfancontrol_available(self, ssh):
        """Test nvfancontrol is available (nvidia-jetpack-tools)."""
        which_result = ssh.run("which nvfancontrol", fail_on_rc=False)
        if not which_result.stdout.strip():
            pytest.skip("nvfancontrol not in PATH")
        result = ssh.sudo("nvfancontrol -q", fail_on_rc=False)
        assert result.exit_status == 0, f"nvfancontrol failed: {result.stderr}"
