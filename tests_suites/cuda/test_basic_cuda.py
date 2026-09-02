"""
CUDA tests for Jetson RPMs.
- PyTorch container: multiply two random tensors on the GPU.
- L4T container: all non-graphical cuda-samples (auto-discovered), cuDNN conv_sample.
- TensorFlow container: GPU device validation.
"""
import pytest
import os
from pathlib import Path
from logging import getLogger
logger = getLogger(__name__)
from tests_resources.container_ops import (
    build_container_image, run_container, cleanup_container_image,
    L4T_JETPACK_IMAGE,
)

FILE = Path(os.path.realpath(__file__)).parent

# Configurable CUDA samples version (latest v12.x, backward compatible with CUDA 12.6 in L4T)
CUDA_SAMPLES_VERSION = os.getenv("CUDA_SAMPLES_VERSION", "v12.5")
# Target sample categories to build (space-separated)
# Default: 1_Utilities + 0_Introduction (core validation, fast to build)
# Extended: CUDA_SAMPLES_TARGETS="1_Utilities 0_Introduction 3_CUDA_Features 6_Performance"
CUDA_SAMPLES_TARGETS = os.getenv("CUDA_SAMPLES_TARGETS", "1_Utilities 0_Introduction")

class TestCUDA:
    """Test CUDA functionality on Jetson devices."""

    @pytest.fixture(scope="class")
    def l4t_cuda_image(self, ssh):
        """Build L4T CUDA samples image once per class, clean up after."""
        tag = f"l4t-cuda-tests:{L4T_JETPACK_IMAGE.split(':')[1]}-{CUDA_SAMPLES_VERSION}"
        build_container_image(
            ssh, FILE / "Dockerfile.l4t_cuda_samples", tag,
            build_args={
                "CUDA_SAMPLES_VERSION": CUDA_SAMPLES_VERSION,
                "CUDA_SAMPLES_TARGETS": CUDA_SAMPLES_TARGETS,
            },
            suite_name="cuda",
        )
        yield tag
        # Teardown
        cleanup_container_image(ssh, tag)

    @pytest.mark.critical
    def test_cuda_pytorch_container(self, ssh):
        """Test CUDA with PyTorch in a container (multiply tensors on GPU)."""
        tag = "cuda-pytorch-qe-tests"
        build_container_image(ssh, FILE / "Dockerfile.pytorch", tag, suite_name="cuda-pytorch")
        result = run_container(ssh, tag,
            "python3 -c 'import torch; print(torch.rand(10).cuda() * torch.rand(10).cuda())'")
        cleanup_container_image(ssh, tag)
        assert result.exit_status == 0, f"CUDA PyTorch test failed: {result.stderr}"

    @pytest.mark.critical
    def test_l4t_device_query(self, ssh, l4t_cuda_image):
        """Test CUDA deviceQuery — enumerates GPU properties."""
        result = run_container(ssh, l4t_cuda_image,
            "bash -c 'cd /cuda-samples/Samples/1_Utilities/deviceQuery && ./deviceQuery'")
        assert result.exit_status == 0, f"deviceQuery failed: {result.stderr}"
        assert "Result = PASS" in result.stdout, f"deviceQuery did not pass: {result.stdout}"

    def test_l4t_cuda_samples(self, ssh, l4t_cuda_image):
        """Run all non-graphical CUDA samples that were built in the container.
        Discovers executables from /cuda-samples/Samples/ source tree.
        Runs each sample from its own directory so sdkFindFilePath finds data files."""
        # Discover all built sample executables in the target categories
        find_cmd = " ".join(
            f"/cuda-samples/Samples/{t}" for t in CUDA_SAMPLES_TARGETS.split()
        )
        result = run_container(ssh, l4t_cuda_image,
            f"find {find_cmd} -maxdepth 2 -type f -executable")
        assert result.exit_status == 0, f"Failed to list samples: {result.stderr}"
        logger.info(f"CUDA samples built: {result.stdout}")
        samples = [s.strip() for s in result.stdout.strip().split('\n') if s.strip()]
        assert len(samples) > 0, "No CUDA samples found"

        # Skip deviceQuery (tested separately as critical test above)
        samples = [s for s in samples if not s.endswith("/deviceQuery")]

        failed = []
        for sample_path in samples:
            sample_name = os.path.basename(sample_path)
            sample_dir = os.path.dirname(sample_path)
            # Run from sample's own directory so sdkFindFilePath finds .fatbin, .cu, .pgm files
            res = run_container(ssh, l4t_cuda_image,
                f"bash -c 'cd {sample_dir} && ./{sample_name}'")
            if res.exit_status != 0:
                failed.append(f"FAILED ON SAMPLE: {sample_name} - {res.stdout.strip().split(chr(10))[-1]} | (exit {res.exit_status}): {res.stderr[:200]}")
        assert not failed, f"{len(failed)} CUDA sample(s) failed:\n" + "\n".join(failed)

    def test_l4t_cudnn_conv_sample(self, ssh, l4t_cuda_image):
        """Test cuDNN conv_sample — validates cuDNN convolution."""
        result = run_container(ssh, l4t_cuda_image,
            "bash -c 'cd /usr/src/cudnn_samples_v*/conv_sample && ./conv_sample'")
        assert result.exit_status == 0, f"cuDNN conv_sample failed: {result.stderr}"
        assert "Test PASSED" in result.stdout, f"cuDNN conv_sample did not pass: {result.stdout}"

    def test_l4t_tensorflow_gpu(self, ssh):
        """Test TensorFlow GPU access via TensorFlow container (deviceQuery)."""
        tag = "cuda-tensorflow-qe-tests"
        build_container_image(ssh, FILE / "Dockerfile.tensorflow", tag, suite_name="cuda-tensorflow")
        result = run_container(ssh, tag, "deviceQuery", timeout=300)
        cleanup_container_image(ssh, tag)
        assert result.exit_status == 0, f"TensorFlow GPU test failed: {result.stderr}"
        assert "Result = PASS" in result.stdout, f"TensorFlow deviceQuery did not pass: {result.stdout}"
