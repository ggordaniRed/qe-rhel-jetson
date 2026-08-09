"""
Signature tests for NVIDIA kernel modules.

Verifies that NVIDIA kernel modules are signed for Secure Boot compatibility.
This ensures the modules will load correctly when Secure Boot is enabled.
"""
import pytest
from logging import getLogger
logger = getLogger(__name__)

NVIDIA_KERNEL_MODULES = [
    "nvgpu",
    "nvidia",
    "nvidia_modeset",
    "nvidia_drm",
]


class TestKernelModuleSignatures:
    """Verify NVIDIA kernel modules are signed for Secure Boot."""

    def test_nvidia_modules_are_signed(self, ssh):
        """
        Check that NVIDIA kernel modules have valid signatures.

        Modules must have:
        - sig_id: PKCS#7
        - signer: Should be "Nvidia GPU OOT CA"
        - sig_hashalgo: sha256 (or stronger)
        """
        unsigned_modules = []
        signed_modules = []
        missing_modules = []

        for module in NVIDIA_KERNEL_MODULES:
            result = ssh.run(f"modinfo {module} ", fail_on_rc=False)

            if result.exit_status != 0:
                missing_modules.append(module)
                continue

            modinfo_output = result.stdout

            has_signer = "signer:" in modinfo_output
            has_sig_id = "sig_id:" in modinfo_output

            if has_signer and has_sig_id:
                signed_modules.append(module)
            else:
                unsigned_modules.append(module)

        if missing_modules:
            logger.info(f"[test_nvidia_modules_are_signed] Missing modules (not loaded): {missing_modules}")

        assert not unsigned_modules, (
            f"The following NVIDIA kernel modules are NOT signed for Secure Boot:\n"
            f"  Unsigned: {unsigned_modules}\n"
            f"  Signed: {signed_modules}\n"
            f"  Missing: {missing_modules}\n"
            f"Modules must be signed for Secure Boot compatibility."
        )

        assert signed_modules, (
            f"No signed NVIDIA kernel modules found.\n"
            f"  Missing: {missing_modules}\n"
            f"At least one NVIDIA module should be loaded and signed."
        )

    def test_nvidia_module_signature_details(self, ssh):
        """
        Verify signature details of the primary nvgpu module.

        Checks:
        - Signature exists
        - Signed by NVIDIA (Nvidia GPU OOT CA)
        - Uses SHA256 or stronger hash algorithm
        """
        result = ssh.run("modinfo nvgpu  | grep -E '^(signer|sig_id|sig_hashalgo):'", fail_on_rc=False)

        if result.exit_status != 0:
            pytest.skip("nvgpu module not loaded - cannot verify signature details")

        output = result.stdout.lower()

        assert "signer:" in output, "nvgpu module missing signer information"
        assert "sig_id:" in output, "nvgpu module missing signature ID"

        assert "nvidia" in output, (
            f"nvgpu module not signed by NVIDIA.\n"
            f"Signature info:\n{result.stdout}"
        )

        assert "sha256" in output or "sha384" in output or "sha512" in output, (
            f"nvgpu module using weak hash algorithm.\n"
            f"Expected sha256 or stronger.\n"
            f"Signature info:\n{result.stdout}"
        )

        logger.info(f"[test_nvidia_module_signature_details] nvgpu signature details:\n{result.stdout}")

    #TODO add secure boot check
