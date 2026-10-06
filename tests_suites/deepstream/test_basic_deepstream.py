"""
DeepStream tests for Jetson RPMs.
- Version check: deepstream-app --version-all
- GStreamer plugin registration: nvinfer, nvstreammux, nvdsosd, nvvideoconvert
- Hardware video conversion pipeline: nvvideoconvert with synthetic source
- Sample inference pipeline (extra): nvinfer on bundled sample stream (marks @extra due to TRT compile time)
"""
import os
import pytest
from pathlib import Path
from logging import getLogger
from tests_suites import conftest as _conftest
from tests_resources.container_ops import (
    build_container_image, run_container, cleanup_container_image,
)

logger = getLogger(__name__)

FILE = Path(os.path.realpath(__file__)).parent

# Jetson samples image (has sample streams/models). Not the dGPU Triton image:
# nvcr.io/nvidia/deepstream:7.1-triton-multiarch prints driver 560.28+ UNAVAILABLE
# on L4T and is the wrong default for this suite.
DEFAULT_DEEPSTREAM_IMAGE = "nvcr.io/nvidia/deepstream:7.1-samples-multiarch"
DEEPSTREAM_IMAGE = os.getenv("DEEPSTREAM_IMAGE", DEFAULT_DEEPSTREAM_IMAGE)

DS_BASE = "/opt/nvidia/deepstream/deepstream"
DS_SAMPLES = f"{DS_BASE}/samples"
DS_STREAMS = f"{DS_SAMPLES}/streams"
DS_CONFIGS = f"{DS_SAMPLES}/configs/deepstream-app"

# Plugins that must be registered for DeepStream pipelines to work
REQUIRED_PLUGINS = [
    "nvinfer",
    "nvstreammux",
    "nvdsosd",
    "nvvideoconvert",
]

INCOMPATIBLE_L4T39_PLUGINS = {"nvstreammux", "nvvideoconvert"}


def _default_image_has_l4t39_glib_mismatch():
    """The DS 7.1 image has GLib 2.76; L4T 39 RHEL libraries need 2.80."""
    return (
        DEEPSTREAM_IMAGE == DEFAULT_DEEPSTREAM_IMAGE
        and str(_conftest.L4T_VERSION or "").startswith("39.")
    )


def _skip_incompatible_l4t39_video_plugins():
    if _default_image_has_l4t39_glib_mismatch():
        pytest.skip(
            "DeepStream 7.1 multiarch is not compatible with L4T 39/RHEL 10: "
            "host libnvdsbufferpool requires GLib 2.80, container provides 2.76"
        )


class TestDeepStream:
    """Test NVIDIA DeepStream SDK on Jetson devices."""

    @pytest.fixture(scope="class")
    def deepstream_image(self, ssh):
        """Pull/build DeepStream container image once per class, clean up after."""
        tag = f"deepstream-qe-tests:{DEEPSTREAM_IMAGE.split(':')[-1]}"
        build_container_image(
            ssh, FILE / "Dockerfile.deepstream", tag,
            build_args={"DEEPSTREAM_IMAGE": DEEPSTREAM_IMAGE},
            timeout=1200,
            suite_name="deepstream",
        )
        yield tag
        cleanup_container_image(ssh, tag)

    @pytest.mark.critical
    def test_deepstream_version(self, ssh, deepstream_image):
        """Verify DeepStream is installed and reports a valid version."""
        result = run_container(ssh, deepstream_image, "deepstream-app --version-all")
        assert result.exit_status == 0, f"deepstream-app --version-all failed: {result.stderr}"
        output = result.stdout + result.stderr
        assert "DeepStream" in output, f"Expected 'DeepStream' in output: {output}"
        logger.info("DeepStream version output:\n%s", output)

    @pytest.mark.critical
    @pytest.mark.parametrize("plugin", REQUIRED_PLUGINS)
    def test_deepstream_gst_plugin(self, ssh, deepstream_image, plugin):
        """Verify each compatible DeepStream GStreamer plugin is registered."""
        if (
            plugin in INCOMPATIBLE_L4T39_PLUGINS
            and _default_image_has_l4t39_glib_mismatch()
        ):
            _skip_incompatible_l4t39_video_plugins()
        result = run_container(ssh, deepstream_image, f"gst-inspect-1.0 {plugin}")
        assert result.exit_status == 0, (
            f"Missing DeepStream GStreamer plugin {plugin}: "
            f"{result.stderr.strip()[:500]}"
        )
        logger.info("Plugin OK: %s", plugin)

    def test_nvvideoconvert_pipeline(self, ssh, deepstream_image):
        """Run a basic nvvideoconvert pipeline on a synthetic source.
        Validates that the DeepStream GPU video-conversion element works end-to-end."""
        _skip_incompatible_l4t39_video_plugins()
        result = run_container(
            ssh, deepstream_image,
            "gst-launch-1.0 videotestsrc num-buffers=30 ! nvvideoconvert ! fakesink",
        )
        assert result.exit_status == 0, (
            f"nvvideoconvert pipeline failed: {result.stderr}"
        )

    def test_nvstreammux_pipeline(self, ssh, deepstream_image):
        """Run a pipeline through nvstreammux — the DeepStream batch multiplexer."""
        _skip_incompatible_l4t39_video_plugins()
        result = run_container(
            ssh, deepstream_image,
            "gst-launch-1.0 "
            "nvstreammux name=mux batch-size=1 width=1280 height=720 "
            "batched-push-timeout=40000 ! fakesink "
            "videotestsrc num-buffers=30 ! nvvideoconvert ! "
            "'video/x-raw(memory:NVMM),width=1280,height=720' ! mux.sink_0",
        )
        assert result.exit_status == 0, (
            f"nvstreammux pipeline failed: {result.stderr}"
        )

    def test_deepstream_sample_inference(self, ssh, deepstream_image):
        """Run DeepStream inference pipeline on the bundled sample H264 stream.
        Uses the Primary_Detector (ResNet10) model from DeepStream samples.
        Note: TRT engine compilation on first run can take several minutes.
        """
        _skip_incompatible_l4t39_video_plugins()

        # L4T / JetPack drivers are 540.x; some NGC images still print
        # "built for NVIDIA Driver Release 560.28+" / UNAVAILABLE. NVIDIA
        # documents that this is a container-runtime banner and is not a
        # functional skip on Jetson — continue and let the pipeline decide.
        probe = run_container(ssh, deepstream_image, "echo ok")
        probe_out = probe.stdout + probe.stderr
        if "UNAVAILABLE" in probe_out:
            logger.warning(
                "DeepStream image printed a driver-compatibility banner "
                "(ignored on Jetson L4T):\n%s",
                probe_out[-600:],
            )

        # Detect available H264 decoder
        # Priority: nvv4l2decoder (L4T native) > nvdec_h264 (NVDEC HW) > avdec_h264 (FFmpeg SW)
        decoders_to_try = ["nvv4l2decoder", "nvdec_h264", "avdec_h264", "nvh264dec"]
        decoder = None
        for dec in decoders_to_try:
            dec_check = run_container(ssh, deepstream_image, f"gst-inspect-1.0 {dec}")
            if dec_check.exit_status == 0:
                decoder = dec
                break
        if not decoder:
            pytest.skip("No H264 decoder found in DeepStream container")
        logger.info("Using H264 decoder: %s", decoder)

        sample_stream = f"{DS_STREAMS}/sample_720p.h264"
        infer_config  = f"{DS_CONFIGS}/config_infer_primary.txt"

        result = run_container(
            ssh, deepstream_image,
            f"bash -c 'cd {DS_BASE} && "
            f"gst-launch-1.0 "
            f"nvstreammux name=mux batch-size=1 width=1280 height=720 "
            f"batched-push-timeout=40000 ! "
            f"nvinfer config-file-path={infer_config} batch-size=1 ! "
            f"fakesink "
            f"filesrc location={sample_stream} ! h264parse ! {decoder} ! "
            f"mux.sink_0'",
            timeout=900,
        )
        assert result.exit_status == 0, (
            "DeepStream inference pipeline failed:\n"
            + (result.stdout + result.stderr)[-4000:]
        )
        logger.info("DeepStream inference pipeline output tail:\n%s",
                    (result.stdout + result.stderr)[-800:])
