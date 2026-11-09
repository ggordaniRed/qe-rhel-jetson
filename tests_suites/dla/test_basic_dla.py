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


class TestDLA:
    """Test DLA functionality on Jetson devices."""

    @pytest.fixture(scope="class")
    def dla_runtime_available(self, ssh):
        """Skip DLA tests when hardware or the JetPack release lacks DLA support."""
        spec = _conftest.get_hardware_spec(_conftest.HARDWARE_MODEL_NAME)
        if not spec.get("dla", {}).get("supported"):
            pytest.skip("DLA is not supported on this hardware")

        jetpack = str(_conftest.JETPACK_VERSION or "")
        l4t = str(_conftest.L4T_VERSION or "")
        if jetpack in {"7.2", "7.2.1"} or l4t in {"39.2", "39.2.1"}:
            pytest.skip(
                "DLA is unavailable on JetPack 7.2 / L4T 39.2. NVIDIA says DLA "
                "support will arrive in a future TensorRT release."
            )

        result = ssh.run(
            "ls /usr/lib64/nvidia/libnvdla_compiler.so* 2>/dev/null",
            fail_on_rc=False,
        )
        if result.exit_status != 0 or not result.stdout.strip():
            pytest.skip(
                "The host does not provide /usr/lib64/nvidia/libnvdla_compiler.so. "
                "TensorRT's plugin cannot load in the container without this "
                "JetPack DLA runtime library."
            )

    @pytest.fixture(scope="class")
    def l4t_dla_tensorrt_image(self, dla_runtime_available, l4t_tensorrt_image):
        """Build/use the TensorRT image only when DLA is supported."""
        return l4t_tensorrt_image

    @pytest.fixture(scope="class")
    def l4t_tensorrt_image(self, ssh, l4t_image_pulled):
        """Build L4T TensorRT image once per class, clean up after."""
        tag = f"l4t-tensorrt-tests:{get_l4t_jetpack_image().split(':')[1]}"
        build_container_image(ssh, FILE / "Dockerfile.l4t_tensorrt", tag, suite_name="dla")
        yield tag
        # Teardown
        cleanup_container_image(ssh, tag)

    @pytest.mark.critical
    def test_dla_OnnxMNIST_sample(self, ssh, dla_runtime_available):
        """Test DLA with sample of ONNX model (OnnxMNIST) in TensorRT container (--useDLACore=0)."""
        tag = "dla-tensorrt-qe-tests"
        build_container_image(ssh, FILE / "Dockerfile.tensorrt", tag, suite_name="dla-tensorrt")
        result = run_container(ssh, tag,
            "/workspace/tensorrt/bin/sample_onnx_mnist --useDLACore=0")
        cleanup_container_image(ssh, tag)
        assert result.exit_status == 0, f"DLA test failed, RC not 0: {result.stderr}"
        assert "[V] [TRT] [DlaLayer]" in result.stdout, (
            f"DLA test failed - expected DlaLayer in output: {result.stderr}"
        )

    @pytest.mark.critical
    def test_l4t_trtexec_dla(self, ssh, l4t_dla_tensorrt_image):
        """Test TensorRT via trtexec with ResNet50 model on all DLA cores.
        Iterates over all expected amount of DLA cores by jetson_hardware_specs.yaml. (Skips if DLA not supported)"""
        spec = _conftest.get_hardware_spec(_conftest.HARDWARE_MODEL_NAME)
        cores = spec.get("dla").get("cores")
        for core in range(cores):
            result = run_container(ssh, l4t_dla_tensorrt_image,
                f"/usr/src/tensorrt/bin/trtexec --onnx=/usr/src/tensorrt/data/resnet50/ResNet50.onnx "
                f"--useDLACore={core} --allowGPUFallback --fp16")
            assert result.exit_status == 0, f"TensorRT DLA core {core} failed: {result.stderr}"

    def test_l4t_trtexec_gpu(self, ssh, l4t_tensorrt_image):
        """Test TensorRT on GPU when its image and plugin runtime are available."""
        result = run_container(ssh, l4t_tensorrt_image,
            "/usr/src/tensorrt/bin/trtexec --onnx=/usr/src/tensorrt/data/resnet50/ResNet50.onnx --fp16")
        assert result.exit_status == 0, f"TensorRT GPU test failed: {result.stderr}"
