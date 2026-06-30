"""
CAN bus tests for Jetson RPMs.
Based on test_basic.py and test_basic_locally.py from edge-ai-image-pipelines.
"""
import pytest
import time
import warnings

def wait_for_interface_state(ssh, interface_name, interface_state, timeout=10):
    """Wait for the interface to reach the desired state."""
    result = None
    start_time = time.time()
    while time.time() - start_time < timeout:
        result = ssh.sudo(f"ip link show {interface_name} | grep -Po 'state {interface_state}'", fail_on_rc=False)
        if interface_state in result.stdout:
            return result
        time.sleep(1)
    raise TimeoutError(
        f"Interface {interface_name} did not reach state {interface_state} "
        f"within {timeout} seconds"
    )

class TestCANBus:
    """Test CAN bus functionality on Jetson devices."""

    # available on all Jetson modules (with or without Developer Kit board) because it's a built-in feature of the SoC.
    def test_can(self, ssh):
        """Test CAN bus interfaces are present."""
        result = ssh.sudo("ip -o link show type can")
        assert len(result.stdout.splitlines()) > 0, "No CAN bus interfaces found (should be part of Jetson SoC)"

    def test_can_loopback(self, ssh):
        """
        Test CAN bus loopback:
        You can perform a loopback test to determine whether the controller is working.

        since we don't have a physical transceiver on the board, we can't test the full CAN bus connection.
        CAN controller (on SOC) <--> Transceiver (on Board) <--> device (via external interface - eg USB etc)
        The loopback test is a simple way to test the CAN bus connection without the transceiver,
        which test only the CAN controller (which is part of Jetson SoC).
        """
        # get the first CAN interface that is not UP
        can_interface = ssh.sudo(r"ip -o link show type can | grep -v UP | grep -Po 'can\d+' | head -n 1").stdout.strip()
        if can_interface == "":
            warnings.warn(UserWarning("Not found CAN interface that is not UP, skipping loopback test")) # for loopabck test we need a interface that is not in use
            pytest.skip("Not found CAN interface that is not UP, skipping loopback test")
        original_interface_state = ssh.sudo(f"ip link show {can_interface} | grep -Po 'state \\w+' | cut -d ' ' -f 2").stdout.strip()

        dump_log = "/tmp/candump_loopback.log"
        try:
            ssh.sudo("dnf install can-utils -y") # for candump and cansend cli tools
            # enable CAN driver
            ssh.sudo(f"ip link set {can_interface} type can bitrate 500000 loopback off") # verify the loopback is off before enabling it
            ssh.sudo(f"ip link set {can_interface} type can bitrate 500000 loopback on")
            ssh.sudo(f"ip link set {can_interface} up")
            result = wait_for_interface_state(ssh, can_interface, "UP")
            assert "UP" in result.stdout, "CAN interface is not up"
            # send and receive CAN messages: candump to a log file so we can assert on it without holding the SSH channel open
            ssh.run(f"candump {can_interface} >{dump_log} 2>&1 &")
            time.sleep(0.5)  # let candump start
            ssh.sudo(f"cansend {can_interface} 123#abcdabcd")
            time.sleep(0.5)  # let the frame be received and written
            result = ssh.sudo(f"cat {dump_log}")
            assert "123" in result.stdout, "CAN message is not received as expected"
            assert "AB CD AB CD" in result.stdout, "CAN message is not received as expected"
        finally:
            # Always restore CAN interface state, even if assertions fail
            ssh.sudo("pkill -f candump", fail_on_rc=False)
            ssh.sudo(f"rm -f {dump_log}", fail_on_rc=False)
            ssh.sudo("pkill -f cansend", fail_on_rc=False)
            ssh.sudo(f"ip link set {can_interface} {original_interface_state.lower()}")
            try:
                wait_for_interface_state(ssh, can_interface, original_interface_state)
            except TimeoutError:
                warnings.warn(UserWarning(f"Could not restore CAN interface {can_interface} to {original_interface_state}"))
            ssh.sudo(f"ip link set {can_interface} type can bitrate 500000 loopback off")
