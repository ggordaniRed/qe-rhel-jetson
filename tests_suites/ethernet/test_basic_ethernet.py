"""
Ethernet tests for Jetson RPMs.
"""

class TestEthernet:
    """Test Ethernet functionality on Jetson devices."""

    def test_ethernet_interfaces(self, ssh):
        """Test Ethernet network interfaces are present."""
        result = ssh.sudo("nmcli -t -f DEVICE,TYPE device | grep ethernet")
        assert len(result.stdout.splitlines()) > 0, "No Ethernet interfaces found"

    def test_ethernet_driver_loaded(self, ssh):
        """Test Ethernet driver is loaded."""
        result = ssh.run("lsmod | grep -iE 'eqos|stmmac|dwmac|r8169|e1000|igb|realtek|xgbe|nfp'", fail_on_rc=False)
        # Ethernet drivers may have various names, so we just check if command succeeds
        assert result.exit_status == 0, f"Failed to check Ethernet drivers: {result.stderr}"
