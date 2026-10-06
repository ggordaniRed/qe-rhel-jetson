"""
DLA (Deep Learning Accelerator) tests for Jetson RPMs.
- TensorRT sampleOnnxMNIST with --useDLACore=0 (tensorrt:25.09-py3-igpu container).
- L4T container: trtexec on GPU and all DLA cores.

TODO: consider adding OE4T TensorRT tests (algorithm_selector, char-rnn, dynamic_reshape, int8_api, io_formats)
      https://github.com/OE4T/meta-tegra-community/tree/master/recipes-test/tegra-tests/tensorrt-tests
"""
import pytest
import os
from pathlib import Path
from tests_suites import conftest as _conftest
from tests_resources.container_ops import (
    build_container_image, run_container, cleanup_container_image,
    get_l4t_jetpack_image,
)

FILE = Path(os.path.realpath(__file__)).parent

JP72_DLA_LIMITATION = (
    "The stock JetPack 7.2/7.2.1 SBSA stack (CUDA 13.2, TensorRT 10.16) "
    "does not provide the DLA userspace compiler/runtime on Jetson Orin; "
    "libnvdla_compiler.so is absent. DLA requires a future validated "
    "NVIDIA stack packaged for RHEL."
)


def _require_dla_runtime(ssh):
    """Skip DLA-only tests when the stock JetPack 7 runtime is unavailable."""
    result = ssh.run(
        "ls /usr/lib64/nvidia/libnvdla_compiler.so* 2>/dev/null",
        fail_on_rc=False,
    )
    if result.exit_status != 0 or not result.stdout.strip():
        pytest.skip(JP72_DLA_LIMITATION)


class TestDLA:
    """Test DLA functionality on Jetson devices."""

    @pytest.fixture(scope="class")
    def dla_runtime_available(self, ssh):
        """Gate DLA fixtures without suppressing TensorRT GPU validation."""
        _require_dla_runtime(ssh)

    @pytest.fixture(scope="class")
    def l4t_tensorrt_image(self, ssh):
        """Build L4T TensorRT image once per class, clean up after."""
        image = get_l4t_jetpack_image()
        exists = ssh.sudo(f"podman image exists {image}", fail_on_rc=False)
        if exists.exit_status != 0:
            pytest.skip(
                f"L4T image is not cached: {image}. Automatic NGC pulls are disabled."
            )
        tag = f"l4t-tensorrt-tests:{image.split(':')[1]}"
        build_container_image(ssh, FILE / "Dockerfile.l4t_tensorrt", tag, suite_name="dla")
        yield tag
        # Teardown
        cleanup_container_image(ssh, tag)

    @pytest.mark.critical
    def test_dla_OnnxMNIST_sample(self, ssh, dla_runtime_available):
        """Test DLA with sample of ONNX model (OnnxMNIST) in TensorRT container (--useDLACore=0)."""
        spec = _conftest.get_hardware_spec(_conftest.HARDWARE_MODEL_NAME)
        tag = "dla-tensorrt-qe-tests"
        build_container_image(ssh, FILE / "Dockerfile.tensorrt", tag, suite_name="dla-tensorrt")
        try:
            result = run_container(ssh, tag,
                "/workspace/tensorrt/bin/sample_onnx_mnist --useDLACore=0")
        finally:
            cleanup_container_image(ssh, tag)
        if not spec.get("dla").get("supported"):
          assert result.exit_status != 0, "DLA test passed, but not supported (see jetson_hardware_specs.yaml)"
        else:
          assert result.exit_status == 0, f"DLA test failed, RC not 0: {result.stderr}"
          assert ("[V] [TRT] [DlaLayer]" in result.stdout
          ), f"DLA test failed - expected DlaLayer in output: {result.stderr}"

    @pytest.mark.critical
    def test_l4t_trtexec_dla(
        self, ssh, dla_runtime_available, l4t_tensorrt_image
    ):
        """Test TensorRT via trtexec with ResNet50 model on all DLA cores.
        Iterates over all expected amount of DLA cores by jetson_hardware_specs.yaml. (Skips if DLA not supported)"""
        spec = _conftest.get_hardware_spec(_conftest.HARDWARE_MODEL_NAME)
        if not spec.get("dla").get("supported"):
            pytest.skip("DLA not supported on this hardware")
        cores = spec.get("dla").get("cores")
        for core in range(cores):
            result = run_container(ssh, l4t_tensorrt_image,
                f"/usr/src/tensorrt/bin/trtexec --onnx=/usr/src/tensorrt/data/resnet50/ResNet50.onnx "
                f"--useDLACore={core} --allowGPUFallback --fp16")
            assert result.exit_status == 0, f"TensorRT DLA core {core} failed: {result.stderr}"

    def test_l4t_trtexec_gpu(self, ssh, l4t_tensorrt_image):
        """Test TensorRT via trtexec on GPU only (no DLA). Works on all Jetson hardware."""
        result = run_container(ssh, l4t_tensorrt_image,
            "/usr/src/tensorrt/bin/trtexec --onnx=/usr/src/tensorrt/data/resnet50/ResNet50.onnx --fp16")
        assert result.exit_status == 0, f"TensorRT GPU test failed: {result.stderr}"
