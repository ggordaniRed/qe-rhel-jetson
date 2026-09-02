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
from tests_resources.container_ops import (
    build_container_image, run_container, cleanup_container_image,
)

logger = getLogger(__name__)

FILE = Path(os.path.realpath(__file__)).parent

DEEPSTREAM_IMAGE = os.getenv("DEEPSTREAM_IMAGE", "nvcr.io/nvidia/deepstream:7.1-triton-multiarch")

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
    def test_deepstream_gst_plugins(self, ssh, deepstream_image):
        """Verify required DeepStream GStreamer plugins are registered."""
        failed = []
        for plugin in REQUIRED_PLUGINS:
            result = run_container(ssh, deepstream_image, f"gst-inspect-1.0 {plugin}")
            if result.exit_status != 0:
                failed.append(f"{plugin}: {result.stderr.strip()[:120]}")
            else:
                logger.info("Plugin OK: %s", plugin)
        assert not failed, "Missing DeepStream GStreamer plugins:\n" + "\n".join(failed)

    def test_nvvideoconvert_pipeline(self, ssh, deepstream_image):
        """Run a basic nvvideoconvert pipeline on a synthetic source.
        Validates that the DeepStream GPU video-conversion element works end-to-end."""
        result = run_container(
            ssh, deepstream_image,
            "gst-launch-1.0 videotestsrc num-buffers=30 ! nvvideoconvert ! fakesink",
        )
        assert result.exit_status == 0, (
            f"nvvideoconvert pipeline failed: {result.stderr}"
        )

    def test_nvstreammux_pipeline(self, ssh, deepstream_image):
        """Run a pipeline through nvstreammux — the DeepStream batch multiplexer."""
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

    @pytest.mark.extra
    def test_deepstream_sample_inference(self, ssh, deepstream_image):
        """Run DeepStream inference pipeline on the bundled sample H264 stream.
        Uses the Primary_Detector (ResNet10) model from DeepStream samples.
        Marked @extra — TRT engine compilation on first run can take several minutes.
        Run with: pytest --run-extra tests_suites/deepstream/

        Note: requires a Jetson L4T-compatible DeepStream image.
        Set DEEPSTREAM_IMAGE=nvcr.io/nvidia/deepstream-l4t:<version> for Jetson devices.
        """
        # Detect GPU driver compatibility — triton-multiarch requires NVIDIA driver 560.28+
        # while Jetson L4T uses a different driver lineage (e.g. 540.x).
        compat = run_container(ssh, deepstream_image, "echo ok")
        if "UNAVAILABLE" in (compat.stdout + compat.stderr):
            pytest.skip(
                "DeepStream container not compatible with installed NVIDIA driver "
                "(triton-multiarch requires 560.28+, Jetson L4T uses a different version). "
                "Set DEEPSTREAM_IMAGE to a Jetson L4T DeepStream image, e.g. "
                "nvcr.io/nvidia/deepstream-l4t:<version>"
            )

        # Detect available H264 decoder — nvv4l2decoder is L4T-only, not in triton-multiarch
        dec_check = run_container(ssh, deepstream_image, "gst-inspect-1.0 nvv4l2decoder")
        decoder = "nvv4l2decoder" if dec_check.exit_status == 0 else "avdec_h264"
        logger.info("Using H264 decoder: %s", decoder)

        sample_stream = f"{DS_STREAMS}/sample_720p.h264"
        infer_config  = f"{DS_CONFIGS}/config_infer_primary.txt"

        result = run_container(
            ssh, deepstream_image,
            f"bash -c 'cd {DS_BASE} && "
            f"gst-launch-1.0 "
            f"filesrc location={sample_stream} ! h264parse ! {decoder} ! "
            f"nvstreammux name=mux batch-size=1 width=1280 height=720 "
            f"batched-push-timeout=40000 ! "
            f"nvinfer config-file-path={infer_config} batch-size=1 ! "
            f"fakesink'",
            timeout=900,
        )
        assert result.exit_status == 0, (
            f"DeepStream inference pipeline failed: {result.stderr}"
        )
        logger.info("DeepStream inference pipeline output tail:\n%s",
                    (result.stdout + result.stderr)[-800:])
