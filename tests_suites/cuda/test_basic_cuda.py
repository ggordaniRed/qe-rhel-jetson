"""
CUDA tests for Jetson RPMs.
- PyTorch container: multiply two random tensors on the GPU.
- JetPack 7 CUDA samples container: run the binaries installed in /usr/local/bin.
- TensorFlow container: GPU device validation.
"""
import pytest
import os
import shlex
import uuid
from logging import getLogger
logger = getLogger(__name__)
from tests_suites import conftest as suite_conftest
from tests_resources.container_ops import (
    run_container,
)

CUDA_SAMPLES_IMAGE = os.getenv(
    "CUDA_SAMPLES_IMAGE",
    "registry.gitlab.com/redhat/rhel/sst/orin-sidecar/nvidia-jetson-sidecar/cuda-samples:jetpack7-cuda13.2",
)
GITLAB_REGISTRY_USER = (
    os.getenv("GITLAB_REGISTRY_USER")
    or os.getenv("GITLAB_USERNAME")
    or os.getenv("CI_REGISTRY_USER")
)
GITLAB_REGISTRY_TOKEN = (
    os.getenv("GITLAB_REGISTRY_TOKEN")
    or os.getenv("GITLAB_TOKEN")
    or os.getenv("CI_REGISTRY_PASSWORD")
)


def _pull_cuda_samples_image(ssh):
    """Log in to GitLab without logging the token, then pull the test image."""
    image_exists = ssh.sudo(
        f"podman image exists {shlex.quote(CUDA_SAMPLES_IMAGE)}",
        fail_on_rc=False,
    )
    if image_exists.exit_status == 0:
        return

    if not GITLAB_REGISTRY_USER or not GITLAB_REGISTRY_TOKEN:
        pytest.fail(
            "CUDA samples image is private. Set GITLAB_REGISTRY_USER and "
            "GITLAB_REGISTRY_TOKEN (or CI_REGISTRY_USER/CI_REGISTRY_PASSWORD)."
        )

    remote_auth = f"/tmp/cuda-samples-auth-{uuid.uuid4().hex}.json"
    login_cmd = (
        f"podman login --authfile {shlex.quote(remote_auth)} --username "
        f"{shlex.quote(GITLAB_REGISTRY_USER)} --password-stdin registry.gitlab.com"
    )
    try:
        # Use Paramiko directly so the token is sent over stdin and never appears
        # in Fabric's command logging. The test connection runs as root on Beaker.
        stdin, stdout, stderr = ssh.client.exec_command(login_cmd)
        stdin.write(GITLAB_REGISTRY_TOKEN + "\n")
        stdin.flush()
        stdin.channel.shutdown_write()
        login_rc = stdout.channel.recv_exit_status()
        if login_rc != 0:
            pytest.fail("GitLab registry login failed for the CUDA samples image.")

        pull_cmd = (
            f"podman --authfile {shlex.quote(remote_auth)} pull "
            f"{shlex.quote(CUDA_SAMPLES_IMAGE)}"
        )
        pull_result = ssh.sudo(pull_cmd, timeout=900, fail_on_rc=False)
        if pull_result.exit_status != 0:
            pytest.fail(
                "GitLab image pull failed with HTTP 403. Verify that the token "
                "has read_registry permission and access to the project."
            )
    finally:
        ssh.sudo(
            f"rm -f -- {shlex.quote(remote_auth)}",
            fail_on_rc=False,
            print_output=False,
        )


def _skip_jetpack7_ngc_image_test():
    """NGC igpu images are not compatible with the JetPack 7 test host."""
    if str(suite_conftest.L4T_VERSION or "").startswith("39."):
        pytest.skip(
            "NGC PyTorch/TensorFlow igpu images are not Jetson L4T 39 images; "
            "use the private JetPack 7 CUDA-samples image instead."
        )


def _require_cached_image(ssh, image):
    """Prevent podman run from implicitly pulling large incompatible images."""
    result = ssh.sudo(
        f"podman image exists {shlex.quote(image)}",
        fail_on_rc=False,
    )
    if result.exit_status != 0:
        pytest.skip(f"Container image is not cached; automatic pulls are disabled: {image}")


class TestCUDA:
    """Test CUDA functionality on Jetson devices."""

    @pytest.fixture(scope="class")
    def l4t_cuda_image(self, ssh):
        """Use the prebuilt JetPack 7 CUDA samples image."""
        _pull_cuda_samples_image(ssh)
        yield CUDA_SAMPLES_IMAGE

    @pytest.mark.critical
    def test_cuda_pytorch_container(self, ssh):
        """Test CUDA with PyTorch in a container (multiply tensors on GPU)."""
        _skip_jetpack7_ngc_image_test()
        image = "nvcr.io/nvidia/pytorch:25.09-py3-igpu"
        _require_cached_image(ssh, image)
        result = run_container(ssh, image,
            "python3 -c 'import torch; print(torch.rand(10).cuda() * torch.rand(10).cuda())'")
        assert result.exit_status == 0, f"CUDA PyTorch test failed: {result.stderr}"

    @pytest.mark.critical
    def test_l4t_device_query(self, ssh, l4t_cuda_image):
        """Test CUDA deviceQuery — enumerates GPU properties."""
        result = run_container(ssh, l4t_cuda_image, "deviceQuery")
        assert result.exit_status == 0, f"deviceQuery failed: {result.stderr}"
        assert "Result = PASS" in result.stdout, f"deviceQuery did not pass: {result.stdout}"

    def test_l4t_cuda_samples(self, ssh, l4t_cuda_image):
        """Run the CUDA sample binaries provided by the JetPack 7 image."""
        result = run_container(ssh, l4t_cuda_image,
            "find /usr/local/bin -maxdepth 1 -type f -executable "
            "\\( -name deviceQuery -o -name nbody -o "
            "-name p2pBandwidthLatencyTest -o -name bandwidthTest \\)")
        assert result.exit_status == 0, f"Failed to list samples: {result.stderr}"
        logger.info(f"CUDA samples built: {result.stdout}")
        # The NVIDIA entrypoint writes its banner to stdout, so only retain
        # paths emitted by find.
        samples = [
            s.strip()
            for s in result.stdout.strip().split('\n')
            if s.strip().startswith('/usr/local/bin/')
        ]
        assert len(samples) > 0, "No CUDA samples found"

        # Skip deviceQuery (tested separately as critical test above) and the
        # bandwidthTest wrapper, which invokes p2pBandwidthLatencyTest again.
        samples = [
            s for s in samples
            if not s.endswith(("/deviceQuery", "/bandwidthTest"))
        ]

        failed = []
        for sample_path in samples:
            sample_name = os.path.basename(sample_path)
            command = f"{sample_path} -benchmark" if sample_name == "nbody" else sample_path
            res = run_container(ssh, l4t_cuda_image, command)
            if res.exit_status != 0:
                failed.append(f"FAILED ON SAMPLE: {sample_name} - {res.stdout.strip().split(chr(10))[-1]} | (exit {res.exit_status}): {res.stderr[:200]}")
        assert not failed, f"{len(failed)} CUDA sample(s) failed:\n" + "\n".join(failed)

    def test_l4t_cudnn_conv_sample(self, ssh, l4t_cuda_image):
        """Run cuDNN conv_sample when the image provides it."""
        result = run_container(
            ssh,
            l4t_cuda_image,
            "bash -c 'compgen -G \"/usr/src/cudnn_samples_v*/conv_sample\" >/dev/null'",
        )
        if result.exit_status != 0:
            pytest.skip("The JetPack 7 CUDA-samples image does not include cuDNN conv_sample")

        result = run_container(ssh, l4t_cuda_image,
            "bash -c 'cd /usr/src/cudnn_samples_v*/conv_sample && ./conv_sample'")
        assert result.exit_status == 0, f"cuDNN conv_sample failed: {result.stderr}"
        assert "Test PASSED" in result.stdout, f"cuDNN conv_sample did not pass: {result.stdout}"

    def test_l4t_tensorflow_gpu(self, ssh):
        """Test TensorFlow GPU access via TensorFlow container (deviceQuery)."""
        _skip_jetpack7_ngc_image_test()
        image = "nvcr.io/nvidia/tensorflow:24.04-tf2-py3-igpu"
        _require_cached_image(ssh, image)
        result = run_container(ssh, image, "deviceQuery", timeout=300)
        assert result.exit_status == 0, f"TensorFlow GPU test failed: {result.stderr}"
        assert "Result = PASS" in result.stdout, f"TensorFlow deviceQuery did not pass: {result.stdout}"
