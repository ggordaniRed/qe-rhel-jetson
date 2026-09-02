"""
Multimedia tests for Jetson RPMs.
- GStreamer: native hardware video encode/decode pipeline (with OpenH264 CPU fallback for Nano).
- MMAPI: Jetson Multimedia API tests (video, JPEG, convert) via L4T container.
"""
import pytest
import os
import warnings
from pathlib import Path
from tests_suites import conftest as _conftest
from tests_resources.container_ops import (
    build_container_image, run_container, cleanup_container_image,
    L4T_JETPACK_IMAGE,
)

FILE = Path(os.path.realpath(__file__)).parent


class TestMultimedia:
    """Test multimedia functionality on Jetson devices."""

    @pytest.fixture(scope="class")
    def l4t_mmapi_image(self, ssh):
        """Build L4T MMAPI image once per class, clean up after."""
        tag = f"l4t-mmapi-tests:{L4T_JETPACK_IMAGE.split(':')[1]}"
        build_container_image(
            ssh, FILE / "Dockerfile.l4t_mmapi", tag,
            context_files=[FILE / "run-mmapi-tests.sh"],
            timeout=600, suite_name="mmapi",
        )
        yield tag
        # Teardown
        cleanup_container_image(ssh, tag)

    def test_gstreamer_hardware_video(self, ssh):
        """Test native GStreamer hardware video encode/decode pipeline."""
        spec = _conftest.get_hardware_spec(_conftest.HARDWARE_MODEL_NAME)
        if not spec.get("video_enc").get("supported"):
          warnings.warn(UserWarning("Only Video Decoder is supported on this hardware (see jetson_hardware_specs.yaml), running test with Video Decoder only"))
          ssh.sudo("dnf install gstreamer1-plugin-openh264 -y") # for openh264enc CPU encoding
          ssh.sudo("dnf install gstreamer1-plugins-good gstreamer1-plugins-bad-free -y") # for h264parse
          result = ssh.sudo(
            # Using the Cisco OpenH264 software encoder (running on your CPU) instead of the NVIDIA GPU encoder (nvv4l2h264enc)
            "gst-launch-1.0 videotestsrc num-buffers=300 ! openh264enc usage-type=camera complexity=low ! h264parse ! nvv4l2decoder ! fakesink"
          )
        else:
          result = ssh.sudo(
              "gst-launch-1.0 videotestsrc num-buffers=300 ! nvvidconv ! nvv4l2h264enc ! nvv4l2decoder ! fakesink"
              , fail_on_rc=False
          )
        assert result.exit_status == 0, f"Video acceleration test failed: {result.stderr}"

    @pytest.mark.critical
    def test_l4t_multimedia_api(self, ssh, l4t_mmapi_image):
        """Test multimedia API (video encode/decode, JPEG, convert) via L4T container."""
        spec = _conftest.get_hardware_spec(_conftest.HARDWARE_MODEL_NAME)
        # Skip encoder-dependent tests on hardware without encoder (e.g., Nano)
        skip_tests = []
        extra_flags = ""
        if not spec.get("video_enc", {}).get("supported"):
            skip_tests.extend(["video_encode", "video_cuda_enc", "video_dec_trt", "encode_sample"])
            extra_flags = f"-e SKIP_TESTS={','.join(skip_tests)}"

        result = run_container(ssh, l4t_mmapi_image, "/opt/run-mmapi-tests.sh", timeout=600, extra_flags=extra_flags)
        assert result.exit_status == 0, f"MMAPI tests failed (see output above for details): {result.stderr}"
