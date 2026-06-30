"""
SPI and I2C bus tests for Jetson devices.

I2C tests verify that the kernel exposes I2C buses and that internal
devices (PMICs, EEPROMs, etc.) respond on the bus — no physical
modification required.

SPI tests verify kernel module presence and device-node availability.
The loopback test (MOSI shorted to MISO) is skipped automatically when
no loopback wire is detected.
"""
import re
import pytest
from logging import getLogger

logger = getLogger(__name__)


# ---------------------------------------------------------------------------
# I2C Tests
# ---------------------------------------------------------------------------
class TestI2C:
    """Test I2C bus functionality on Jetson devices."""

    def test_i2c_buses_exist(self, ssh):
        """At least one I2C bus device node must be present."""
        result = ssh.sudo("ls /dev/i2c-* 2>/dev/null", fail_on_rc=False)
        assert result.exit_status == 0 and result.stdout.strip(), (
            "No /dev/i2c-* device nodes found. "
            "I2C may not be enabled in the device-tree or pinmux."
        )
        buses = result.stdout.strip().splitlines()
        logger.info("Found %d I2C bus(es): %s", len(buses), ", ".join(buses))

    def test_i2c_tools_installed(self, ssh):
        """Install i2c-tools (i2cdetect, i2cget, etc.)."""
        ssh.sudo("dnf install -y i2c-tools", fail_on_rc=False)
        result = ssh.run("which i2cdetect", fail_on_rc=False)
        assert result.exit_status == 0, (
            "i2cdetect not found after install attempt. "
            "i2c-tools package may be unavailable."
        )

    def test_i2c_list_buses(self, ssh):
        """i2cdetect -l must list at least one I2C adapter."""
        ssh.sudo("dnf install -y i2c-tools", fail_on_rc=False)
        result = ssh.sudo("i2cdetect -l", fail_on_rc=False)
        assert result.exit_status == 0, f"i2cdetect -l failed: {result.stderr}"
        lines = [l for l in result.stdout.splitlines() if l.strip()]
        assert len(lines) > 0, "i2cdetect -l returned no I2C adapters"
        logger.info("I2C adapters:\n%s", result.stdout)

    def test_i2c_scan_detects_devices(self, ssh):
        """
        Scan all I2C buses and verify that at least one device responds.

        Every Jetson SoC has internal I2C peripherals (PMIC, EEPROM, etc.),
        so at least one address should show up as a hex value or UU (driver
        already claimed the device).
        """
        ssh.sudo("dnf install -y i2c-tools", fail_on_rc=False)

        bus_list = ssh.sudo("i2cdetect -l", fail_on_rc=False)
        assert bus_list.exit_status == 0, "Cannot list I2C buses"

        bus_numbers = re.findall(r"i2c-(\d+)", bus_list.stdout)
        assert len(bus_numbers) > 0, "No I2C buses found"

        all_devices: dict[int, list[str]] = {}
        for bus in bus_numbers:
            scan = ssh.sudo(f"i2cdetect -y -r {bus}", fail_on_rc=False)
            if scan.exit_status != 0:
                logger.warning("Bus %s scan failed: %s", bus, scan.stderr)
                continue
            # Parse hex addresses (two-char hex) and UU entries
            devices = re.findall(r"\b([0-9a-fA-F]{2}|UU)\b", scan.stdout)
            # Filter out header values (00-77 column labels)
            real_devices = [
                d for d in devices
                if d != "UU" and d not in ("00", "10", "20", "30", "40", "50", "60", "70")
                or d == "UU"
            ]
            if real_devices:
                all_devices[int(bus)] = real_devices
                logger.info(
                    "Bus %s: %d device(s) at address(es) %s",
                    bus, len(real_devices), ", ".join(f"0x{d}" if d != "UU" else "UU" for d in real_devices),
                )

        assert len(all_devices) > 0, (
            f"No I2C devices detected on any of {len(bus_numbers)} buses. "
            "Internal Jetson peripherals (PMIC/EEPROM) should always respond."
        )
        total = sum(len(v) for v in all_devices.values())
        logger.info("Total I2C devices found: %d across %d bus(es)", total, len(all_devices))

    def test_i2c_kernel_modules(self, ssh):
        """Core I2C kernel modules must be loaded."""
        result = ssh.run("lsmod | grep -E '^i2c'", fail_on_rc=False)
        assert result.exit_status == 0 and result.stdout.strip(), (
            "No i2c kernel modules loaded (expected i2c_core, i2c_dev, etc.)"
        )
        modules = [line.split()[0] for line in result.stdout.splitlines()]
        logger.info("Loaded I2C modules: %s", ", ".join(modules))


# ---------------------------------------------------------------------------
# SPI Tests
# ---------------------------------------------------------------------------
class TestSPI:
    """Test SPI bus functionality on Jetson devices.

    SPI device nodes (/dev/spidevX.Y) are only present when the pinmux
    is configured via device-tree overlays (e.g. jetson-io.py).  On bootc
    images the pinmux may or may not be pre-configured, so tests that
    require device nodes skip gracefully when none are found.
    """

    def test_spi_kernel_module(self, ssh):
        """The spidev kernel module must be loaded or loadable."""
        result = ssh.run("lsmod | grep spidev", fail_on_rc=False)
        if result.exit_status != 0 or not result.stdout.strip():
            # Try loading it
            load = ssh.sudo("modprobe spidev", fail_on_rc=False)
            if load.exit_status != 0:
                pytest.skip(
                    "spidev module not loaded and modprobe failed — "
                    "SPI may not be configured in device-tree"
                )
            logger.info("spidev module loaded via modprobe")
        else:
            logger.info("spidev module already loaded")

    def test_spi_device_nodes(self, ssh):
        """Check for SPI device nodes in /dev/."""
        ssh.sudo("modprobe spidev", fail_on_rc=False)
        result = ssh.run("ls /dev/spidev* 2>/dev/null", fail_on_rc=False)
        if result.exit_status != 0 or not result.stdout.strip():
            pytest.skip(
                "No /dev/spidev* nodes found — SPI pinmux not configured. "
                "Enable via device-tree overlay or jetson-io.py."
            )
        devices = result.stdout.strip().splitlines()
        logger.info("SPI device nodes: %s", ", ".join(devices))
        assert len(devices) > 0

    def test_spi_controller_in_sysfs(self, ssh):
        """At least one SPI controller should be registered in sysfs."""
        result = ssh.run("ls -d /sys/class/spi_master/spi* 2>/dev/null", fail_on_rc=False)
        if result.exit_status != 0 or not result.stdout.strip():
            pytest.skip("No SPI controllers found in /sys/class/spi_master/")
        controllers = result.stdout.strip().splitlines()
        logger.info("SPI controllers: %s", ", ".join(controllers))

        for ctrl in controllers:
            ctrl_name = ctrl.split("/")[-1]
            stats = ssh.run(f"cat {ctrl}/statistics/messages 2>/dev/null", fail_on_rc=False)
            if stats.exit_status == 0:
                logger.info("  %s: %s messages", ctrl_name, stats.stdout.strip())

    def test_spi_loopback(self, ssh):
        """
        SPI loopback test: send data and verify the same bytes are received.

        Requires a physical jumper wire between MOSI and MISO on the 40-pin
        header.  If no loopback is detected (received all zeros or 0xFF),
        the test is skipped rather than failed.
        """
        ssh.sudo("modprobe spidev", fail_on_rc=False)
        spi_devs = ssh.run("ls /dev/spidev* 2>/dev/null", fail_on_rc=False)
        if spi_devs.exit_status != 0 or not spi_devs.stdout.strip():
            pytest.skip("No SPI device nodes — cannot run loopback test")

        spi_dev = spi_devs.stdout.strip().splitlines()[0]
        logger.info("Using SPI device: %s", spi_dev)

        # Use Python one-liner to do SPI xfer via ctypes/ioctl
        # This avoids needing spidev_test binary or pip packages
        loopback_script = r"""
import struct, fcntl, array, sys, os

SPI_IOC_MAGIC = ord('k')
SPI_IOC_MESSAGE_1 = 0x40206B00  # _IOW('k', 0, 32) for 1 transfer on aarch64

dev = sys.argv[1]
tx_data = bytes([0xAA, 0x55, 0x0F, 0xF0, 0xDE, 0xAD])
rx_data = bytearray(len(tx_data))

tx_buf = array.array('B', tx_data)
rx_buf = array.array('B', rx_data)
tx_addr, _ = tx_buf.buffer_info()
rx_addr, _ = rx_buf.buffer_info()

# struct spi_ioc_transfer (64-bit): tx_buf(8) rx_buf(8) len(4) speed(4)
# delay(2) bits(1) cs_change(1) tx_nbits(1) rx_nbits(1) word_delay(2) pad(4)
xfer = struct.pack('QQIIHBBBBHxxxx',
    tx_addr, rx_addr, len(tx_data),
    1000000,  # 1 MHz
    0,        # delay_usecs
    8,        # bits_per_word
    0, 0, 0, 0)

SPI_IOC_MSG = 0x40206B00
fd = os.open(dev, os.O_RDWR)
try:
    fcntl.ioctl(fd, SPI_IOC_MSG, xfer)
    received = list(rx_buf)
    sent = list(tx_data)
    print(f"SENT: {sent}")
    print(f"RECV: {received}")
    if sent == received:
        print("LOOPBACK_OK")
    elif all(b == 0 for b in received) or all(b == 0xFF for b in received):
        print("NO_LOOPBACK")
    else:
        print("MISMATCH")
finally:
    os.close(fd)
"""
        # Write script to device and execute
        ssh.sudo(f"cat > /tmp/spi_loopback.py << 'PYEOF'\n{loopback_script}\nPYEOF")
        result = ssh.sudo(f"python3 /tmp/spi_loopback.py {spi_dev}", fail_on_rc=False)
        ssh.sudo("rm -f /tmp/spi_loopback.py", fail_on_rc=False)

        if result.exit_status != 0:
            pytest.skip(
                f"SPI ioctl failed (permission or device issue): {result.stderr}"
            )

        logger.info("SPI loopback result:\n%s", result.stdout)

        if "NO_LOOPBACK" in result.stdout:
            pytest.skip(
                "SPI loopback not detected (all zeros/0xFF received). "
                "A jumper wire between MOSI and MISO is required."
            )
        elif "LOOPBACK_OK" in result.stdout:
            pass  # success
        elif "MISMATCH" in result.stdout:
            pytest.fail(
                f"SPI data mismatch — sent != received:\n{result.stdout}"
            )
        else:
            pytest.skip(f"Unexpected SPI loopback output:\n{result.stdout}")
