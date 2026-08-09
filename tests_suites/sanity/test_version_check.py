"""
Sanity tests for JetPack version consistency.

Individual target version checks (RHEL, L4T, userspace, kernel, firmware, [except kmod])
are handled by session-level skip logic in conftest.py. These tests verify
cross-component consistency that the skip logic doesn't cover. (along with checking kmod version)
"""
from tests_resources.hardware_info import (
    get_all_jetpack_rpm_versions,
    compare_versions_gte,
)
from tests_suites import conftest
from logging import getLogger
logger = getLogger(__name__)


class TestVersionConsistency:
    """Cross-component version consistency checks."""

    def test_all_userspace_rpms_same_version(self, ssh):
        """All non-kmod nvidia-jetpack RPMs must have the same version as the core RPM."""
        rpm_versions = get_all_jetpack_rpm_versions(ssh)
        logger.info(f"All nvidia-jetpack RPMs: {rpm_versions}")
        assert rpm_versions, "No nvidia-jetpack RPMs found on the device"

        # Separate kmod from userspace components
        userspace = {k: v for k, v in rpm_versions.items() if "kmod" not in k}
        logger.info(f"Userspace nvidia-jetpack RPMs: {userspace}")
        assert userspace, "No userspace nvidia-jetpack RPMs found"

        core_version = userspace.get("core")
        logger.info(f"Jetpack Core version: {core_version}")
        assert core_version is not None, (
            f"nvidia-jetpack core RPM not found. Found components: {list(userspace.keys())}"
        )

        mismatched = {k: v for k, v in userspace.items() if v != core_version}
        logger.info(f"Mismatched userspace RPMs: {mismatched}")
        assert not mismatched, (
            f"Userspace RPM version mismatch (expected {core_version}): {mismatched}"
        )

    def test_kmod_version_gte_userspace(self, ssh):
        """JetPack kmod RPM version must be >= userspace RPM version."""
        logger.info(f"JetPack userspace version: {conftest.JETPACK_VERSION}")
        logger.info(f"JetPack kmod version: {conftest.JETPACK_KMOD_VERSION}")
        assert conftest.JETPACK_VERSION is not None, "JetPack userspace version not detected"
        assert conftest.JETPACK_KMOD_VERSION is not None, "JetPack kmod version not detected"
        assert compare_versions_gte(conftest.JETPACK_KMOD_VERSION, conftest.JETPACK_VERSION), (
            f"JetPack kmod version ({conftest.JETPACK_KMOD_VERSION}) is older than "
            f"userspace version ({conftest.JETPACK_VERSION})"
        )
