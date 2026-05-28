"""
Build validation tests for RC (production) vs stage bootc images.

RC builds (Containerfile.in) omit stage-only packages and have no root password.
Stage builds (Containerfile-stage.in) include rsync, xorg-x11-server-Xorg, and
set a default root password.
"""
import pytest
from tests_suites import conftest
from logging import getLogger
logger = getLogger(__name__)

STAGE_ONLY_PACKAGES = ["rsync", "xorg-x11-server-Xorg"]


class TestRCBuildPackages:
    """Verify RC builds do not ship stage-only packages."""

    @pytest.mark.parametrize("package", STAGE_ONLY_PACKAGES)
    def test_stage_only_package_not_installed(self, ssh, package):
        """Stage-only packages must not be present on RC/production builds."""
        if conftest.IS_STAGE_BUILD:
            pytest.skip("Stage build — expecting stage-only packages to be present")

        result = ssh.run(f"rpm -q {package}", fail_on_rc=False)
        assert result.exit_status != 0, (
            f"{package} is installed on an RC build but should only be in stage images"
        )
        logger.info(f"[RC validation] {package} correctly absent from RC build")


class TestRootAccess:
    """Verify root access matches build type expectations."""

    def test_ssh_password_auth_disabled(self, ssh):
        """RC builds must not allow SSH password login for root.
        Verifies root has no usable password hash in /etc/shadow."""
        if conftest.IS_STAGE_BUILD:
            pytest.skip("Stage build sets a default root password")

        result = ssh.run("getent shadow root | cut -d: -f2", fail_on_rc=False)
        assert result.exit_status == 0, f"Failed to read shadow entry: {result.stderr}"

        password_field = result.stdout.strip()
        logger.info(f"[RC validation] root password field: {password_field!r}")
        assert password_field in ("!!", "!", "*", ""), (
            f"Root has a password hash set ({password_field[:8]}...) — "
            f"RC builds should not allow password-based SSH login"
        )

    def test_root_password_locked(self, ssh):
        """RC builds must have root password locked (key-only access).
        Stage builds set a default root password."""
        result = ssh.run("passwd -S root", fail_on_rc=False)
        assert result.exit_status == 0, f"Failed to query root password status: {result.stderr}"

        status_field = result.stdout.split()[1] if len(result.stdout.split()) > 1 else ""
        logger.info(f"[RC validation] root password status: {status_field}")

        if conftest.IS_STAGE_BUILD:
            assert status_field == "PS", (
                f"Stage build should have root password set (PS), got: {status_field}"
            )
        else:
            assert status_field in ("LK", "NP", "L"), (
                f"RC build should have root password locked, got: {status_field}"
            )
