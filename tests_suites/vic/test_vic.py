"""
VIC (Video Image Compositor) hardware tests for Jetson RPMs.
Tests the VIC hardware engine via GStreamer's nvvidconv plugin:
- Format conversion (raw-YUV <-> NVMM, NVMM <-> NVMM)
- Scaling/resizing with interpolation methods
- Rotation and flip (8 modes)
- Crop/ROI
- Memory layout (block-linear vs pitch-linear)
- Pipeline integration (VIC -> encoder, CUDA -> VIC)

Reference: https://docs.nvidia.com/jetson/archives/r36.4/DeveloperGuide/SD/Multimedia/AcceleratedGstreamer.html
"""
import pytest
from tests_suites import conftest as _conftest


# -- Format lists from NVIDIA docs (VIC on Jetson) --

RAW_TO_NVMM_FORMATS = [
    "I420", "UYVY", "YUY2", "YVYU", "NV12", "NV16", "NV24",
    "GRAY8", "BGRx", "RGBA", "Y42B",
]

NVMM_TO_RAW_FORMATS = [
    "I420", "UYVY", "YUY2", "YVYU", "NV12", "NV16", "NV24",
    "GRAY8", "BGRx", "RGBA", "Y42B",
]

# NVMM->NVMM conversion pairs (representative from each row in NVIDIA's matrix)
NVMM_CONVERSION_PAIRS = [
    ("NV12", "NV24"),
    ("NV24", "NV12"),
    ("I420", "I420"),
    ("UYVY", "BGRx"),
    ("BGRx", "GRAY8"),
    ("YUY2", "RGBA"),
    ("RGBA", "Y42B"),
]

INTERPOLATION_METHODS = [
    pytest.param(0, id="nearest"),
    pytest.param(1, id="bilinear"),
    pytest.param(2, id="5-tap"),
    pytest.param(3, id="10-tap"),
    pytest.param(4, id="smart"),
    pytest.param(5, id="nicest"),
]

FLIP_METHODS = [
    pytest.param(0, id="identity"),
    pytest.param(1, id="ccw-90"),
    pytest.param(2, id="rotate-180"),
    pytest.param(3, id="cw-90"),
    pytest.param(4, id="horizontal-flip"),
    pytest.param(5, id="upper-right-diagonal"),
    pytest.param(6, id="vertical-flip"),
    pytest.param(7, id="upper-left-diagonal"),
]


class TestVIC:
    """Test VIC (Video Image Compositor) hardware engine on Jetson devices."""

    @pytest.fixture(autouse=True)
    def _check_vic_support(self, ssh):
        """Skip all VIC tests if hardware spec says VIC is not supported."""
        spec = _conftest.get_hardware_spec(_conftest.HARDWARE_MODEL_NAME)
        if not spec.get("vic", {}).get("supported"):
            pytest.skip(
                "VIC not supported on this hardware "
                "(see jetson_hardware_specs.yaml)"
            )

    @pytest.fixture(scope="class")
    def gst_plugins_installed(self, ssh):
        """Install GStreamer plugins needed by pipeline integration tests (h264parse, qtmux)."""
        ssh.sudo(
            "dnf install -y gstreamer1-plugins-bad-free gstreamer1-plugins-good",
            fail_on_rc=False,
        )

    # -------------------------------------------------------------------------
    # Hardware check
    # -------------------------------------------------------------------------

    @pytest.mark.critical
    def test_nvvidconv_plugin_available(self, ssh):
        """Verify the nvvidconv GStreamer plugin (VIC interface) is available."""
        result = ssh.sudo("gst-inspect-1.0 nvvidconv", fail_on_rc=False)
        assert result.exit_status == 0, (
            f"nvvidconv GStreamer plugin not found — VIC hardware interface unavailable: {result.stderr}"
        )

    # -------------------------------------------------------------------------
    # Format conversion: raw-YUV -> NVMM (VIC)
    # -------------------------------------------------------------------------

    @pytest.mark.parametrize("fmt", RAW_TO_NVMM_FORMATS)
    def test_raw_to_nvmm_format_conversion(self, ssh, fmt):
        """Test VIC format conversion from raw-YUV input to NVMM output."""
        # GRAY8 converts to I420 in NVMM (per NVIDIA docs)
        nvmm_fmt = "I420" if fmt == "GRAY8" else fmt
        result = ssh.sudo(
            f"gst-launch-1.0 videotestsrc num-buffers=30 ! "
            f"'video/x-raw, format=(string){fmt}, width=(int)640, height=(int)480' ! "
            f"nvvidconv ! "
            f"'video/x-raw(memory:NVMM), format=(string){nvmm_fmt}' ! "
            f"fakesink",
            fail_on_rc=False,
        )
        assert result.exit_status == 0, (
            f"VIC raw-to-NVMM conversion failed for {fmt} -> {nvmm_fmt}: {result.stderr}"
        )

    # -------------------------------------------------------------------------
    # Format conversion: NVMM -> raw-YUV (VIC)
    # -------------------------------------------------------------------------

    @pytest.mark.parametrize("fmt", NVMM_TO_RAW_FORMATS)
    def test_nvmm_to_raw_format_conversion(self, ssh, fmt):
        """Test VIC format conversion from NVMM to raw-YUV output."""
        result = ssh.sudo(
            f"gst-launch-1.0 videotestsrc num-buffers=30 ! "
            f"'video/x-raw, format=(string)NV12, width=(int)640, height=(int)480' ! "
            f"nvvidconv ! 'video/x-raw(memory:NVMM), format=(string)NV12' ! "
            f"nvvidconv ! "
            f"'video/x-raw, format=(string){fmt}' ! "
            f"fakesink",
            fail_on_rc=False,
        )
        assert result.exit_status == 0, (
            f"VIC NVMM-to-raw conversion failed for NV12 -> {fmt}: {result.stderr}"
        )

    # -------------------------------------------------------------------------
    # Format conversion: NVMM -> NVMM (VIC)
    # -------------------------------------------------------------------------

    @pytest.mark.parametrize("src_fmt,dst_fmt", NVMM_CONVERSION_PAIRS,
                             ids=[f"{s}-to-{d}" for s, d in NVMM_CONVERSION_PAIRS])
    def test_nvmm_to_nvmm_format_conversion(self, ssh, src_fmt, dst_fmt):
        """Test VIC NVMM-to-NVMM format conversion (hardware memory)."""
        result = ssh.sudo(
            f"gst-launch-1.0 videotestsrc num-buffers=30 ! "
            f"'video/x-raw, format=(string){src_fmt}, width=(int)640, height=(int)480' ! "
            f"nvvidconv ! 'video/x-raw(memory:NVMM), format=(string){src_fmt}' ! "
            f"nvvidconv ! "
            f"'video/x-raw(memory:NVMM), format=(string){dst_fmt}' ! "
            f"fakesink",
            fail_on_rc=False,
        )
        assert result.exit_status == 0, (
            f"VIC NVMM-to-NVMM conversion failed for {src_fmt} -> {dst_fmt}: {result.stderr}"
        )

    # -------------------------------------------------------------------------
    # Scaling / resizing
    # -------------------------------------------------------------------------

    def test_downscale(self, ssh):
        """Test VIC downscaling from 1280x720 to 640x480."""
        result = ssh.sudo(
            "gst-launch-1.0 videotestsrc num-buffers=30 ! "
            "'video/x-raw, format=(string)I420, width=(int)1280, height=(int)720' ! "
            "nvvidconv ! "
            "'video/x-raw(memory:NVMM), width=(int)640, height=(int)480, format=(string)I420' ! "
            "fakesink",
            fail_on_rc=False,
        )
        assert result.exit_status == 0, f"VIC downscale failed: {result.stderr}"

    def test_upscale(self, ssh):
        """Test VIC upscaling from 640x480 to 1280x720."""
        result = ssh.sudo(
            "gst-launch-1.0 videotestsrc num-buffers=30 ! "
            "'video/x-raw, format=(string)I420, width=(int)640, height=(int)480' ! "
            "nvvidconv ! "
            "'video/x-raw(memory:NVMM), width=(int)1280, height=(int)720, format=(string)I420' ! "
            "fakesink",
            fail_on_rc=False,
        )
        assert result.exit_status == 0, f"VIC upscale failed: {result.stderr}"

    @pytest.mark.parametrize("method", INTERPOLATION_METHODS)
    def test_interpolation_methods(self, ssh, method):
        """Test VIC scaling with each interpolation method (1080p -> 720p)."""
        result = ssh.sudo(
            f"gst-launch-1.0 videotestsrc num-buffers=30 ! "
            f"'video/x-raw, format=(string)NV12, width=(int)1920, height=(int)1080' ! "
            f"nvvidconv interpolation-method={method} ! "
            f"'video/x-raw(memory:NVMM), width=(int)1280, height=(int)720, format=(string)NV12' ! "
            f"fakesink",
            fail_on_rc=False,
        )
        assert result.exit_status == 0, (
            f"VIC interpolation method {method} failed: {result.stderr}"
        )

    # -------------------------------------------------------------------------
    # Rotation and flip
    # -------------------------------------------------------------------------

    @pytest.mark.parametrize("flip_method", FLIP_METHODS)
    def test_rotation_flip(self, ssh, flip_method):
        """Test all 8 VIC rotation/flip modes."""
        result = ssh.sudo(
            f"gst-launch-1.0 videotestsrc num-buffers=30 ! "
            f"'video/x-raw, format=(string)NV12, width=(int)640, height=(int)480' ! "
            f"nvvidconv flip-method={flip_method} ! "
            f"'video/x-raw(memory:NVMM), format=(string)I420' ! "
            f"fakesink",
            fail_on_rc=False,
        )
        assert result.exit_status == 0, (
            f"VIC flip-method={flip_method} failed: {result.stderr}"
        )

    def test_rotation_with_scaling(self, ssh):
        """Test VIC combined rotation (90 CW) + scaling (1080p -> 480x640)."""
        result = ssh.sudo(
            "gst-launch-1.0 videotestsrc num-buffers=30 ! "
            "'video/x-raw, format=(string)NV12, width=(int)1920, height=(int)1080' ! "
            "nvvidconv flip-method=3 ! "
            "'video/x-raw(memory:NVMM), width=(int)480, height=(int)640, format=(string)I420' ! "
            "fakesink",
            fail_on_rc=False,
        )
        assert result.exit_status == 0, (
            f"VIC rotation+scaling failed: {result.stderr}"
        )

    # -------------------------------------------------------------------------
    # Crop / region of interest
    # -------------------------------------------------------------------------

    def test_crop_region(self, ssh):
        """Test VIC crop with left/right/top/bottom properties."""
        result = ssh.sudo(
            "gst-launch-1.0 videotestsrc num-buffers=30 ! "
            "'video/x-raw, format=(string)NV12, width=(int)1920, height=(int)1080' ! "
            "nvvidconv ! 'video/x-raw(memory:NVMM), format=(string)NV12' ! "
            "nvvidconv left=400 right=1520 top=200 bottom=880 ! "
            "'video/x-raw(memory:NVMM), format=(string)I420' ! "
            "fakesink",
            fail_on_rc=False,
        )
        assert result.exit_status == 0, f"VIC crop failed: {result.stderr}"

    # -------------------------------------------------------------------------
    # Memory layout (block-linear vs pitch-linear)
    # -------------------------------------------------------------------------

    def test_pitch_linear_output(self, ssh):
        """Test VIC pitch-linear memory layout output (bl-output=false)."""
        result = ssh.sudo(
            "gst-launch-1.0 videotestsrc num-buffers=1 ! "
            "'video/x-raw, width=(int)640, height=(int)480, format=(string)NV12' ! "
            "nvvidconv bl-output=false ! "
            "'video/x-raw(memory:NVMM)' ! "
            "fakesink",
            fail_on_rc=False,
        )
        assert result.exit_status == 0, (
            f"VIC pitch-linear output (bl-output=false) failed: {result.stderr}"
        )

    def test_block_linear_output(self, ssh):
        """Test VIC block-linear memory layout output (bl-output=true)."""
        result = ssh.sudo(
            "gst-launch-1.0 videotestsrc num-buffers=1 ! "
            "'video/x-raw, width=(int)640, height=(int)480, format=(string)NV12' ! "
            "nvvidconv bl-output=true ! "
            "'video/x-raw(memory:NVMM)' ! "
            "fakesink",
            fail_on_rc=False,
        )
        assert result.exit_status == 0, (
            f"VIC block-linear output (bl-output=true) failed: {result.stderr}"
        )

    # -------------------------------------------------------------------------
    # Pipeline integration
    # -------------------------------------------------------------------------

    def test_vic_format_conversion_to_encoder(self, ssh):
        """Test VIC format conversion feeding into hardware encoder (VIC -> NVENC)."""
        spec = _conftest.get_hardware_spec(_conftest.HARDWARE_MODEL_NAME)
        if not spec.get("video_enc", {}).get("supported"):
            pytest.skip("Video encoder not supported on this hardware")
        result = ssh.sudo(
            "gst-launch-1.0 videotestsrc num-buffers=30 ! "
            "'video/x-raw, format=(string)UYVY, width=(int)1280, height=(int)720' ! "
            "nvvidconv ! "
            "'video/x-raw(memory:NVMM), format=(string)I420' ! "
            "nvv4l2h264enc ! "
            "'video/x-h264, stream-format=(string)byte-stream' ! "
            "fakesink",
            fail_on_rc=False,
        )
        assert result.exit_status == 0, (
            f"VIC -> encoder pipeline failed: {result.stderr}"
        )

    def test_vic_to_jpeg_encode(self, ssh):
        """Test VIC feeding into JPEG encoder with file output."""
        ssh.sudo("rm -f /tmp/vic_test.jpg", fail_on_rc=False)
        result = ssh.sudo(
            "gst-launch-1.0 videotestsrc num-buffers=1 ! "
            "'video/x-raw, width=(int)640, height=(int)480, format=(string)NV12' ! "
            "nvvidconv ! 'video/x-raw(memory:NVMM)' ! "
            "nvjpegenc ! filesink location=/tmp/vic_test.jpg -e",
            fail_on_rc=False,
        )
        assert result.exit_status == 0, (
            f"VIC -> JPEG encode pipeline failed: {result.stderr}"
        )
        verify = ssh.sudo("test -s /tmp/vic_test.jpg", fail_on_rc=False)
        assert verify.exit_status == 0, "VIC -> JPEG encode produced empty or missing file"
        ssh.sudo("rm -f /tmp/vic_test.jpg", fail_on_rc=False)

    def test_vic_to_multi_jpeg_encode(self, ssh):
        """Test VIC producing multiple JPEG files via multifilesink."""
        ssh.sudo("rm -f /tmp/vic_multi_*.jpeg", fail_on_rc=False)
        result = ssh.sudo(
            "gst-launch-1.0 videotestsrc num-buffers=5 ! "
            "'video/x-raw, width=(int)640, height=(int)480, format=(string)NV12' ! "
            "nvvidconv ! 'video/x-raw(memory:NVMM)' ! "
            "nvjpegenc ! multifilesink location=/tmp/vic_multi_%d.jpeg -e",
            fail_on_rc=False,
        )
        assert result.exit_status == 0, (
            f"VIC -> multi-JPEG encode pipeline failed: {result.stderr}"
        )
        count = ssh.sudo(
            "ls -1 /tmp/vic_multi_*.jpeg 2>/dev/null | wc -l",
            fail_on_rc=False,
        )
        assert int(count.stdout.strip()) >= 1, "VIC -> multi-JPEG produced no output files"
        ssh.sudo("rm -f /tmp/vic_multi_*.jpeg", fail_on_rc=False)

    def test_cuda_to_vic_memory_conversion(self, ssh):
        """Test CUDA->VIC memory conversion pipeline (GPU nvvidconv -> VIC nvvidconv -> encoder).
        On integrated GPUs, gst-v4l2 encoder cannot use CUDA memory directly,
        so VIC acts as memory bridge between CUDA and encoder."""
        spec = _conftest.get_hardware_spec(_conftest.HARDWARE_MODEL_NAME)
        if not spec.get("video_enc", {}).get("supported"):
            pytest.skip("Video encoder not supported on this hardware")
        result = ssh.sudo(
            "gst-launch-1.0 videotestsrc num-buffers=30 ! "
            "'video/x-raw, format=(string)NV12, width=(int)1280, height=(int)720' ! "
            "nvvidconv compute-hw=GPU nvbuf-memory-type=nvbuf-mem-cuda-device ! "
            "'video/x-raw, format=(string)I420' ! "
            "nvvidconv compute-hw=VIC nvbuf-memory-type=nvbuf-mem-surface-array ! "
            "'video/x-raw(memory:NVMM)' ! "
            "nvv4l2h264enc ! "
            "'video/x-h264, stream-format=(string)byte-stream' ! "
            "fakesink",
            fail_on_rc=False,
        )
        assert result.exit_status == 0, (
            f"CUDA -> VIC memory conversion pipeline failed: {result.stderr}"
        )

    def test_vic_format_conversion_in_transcode(self, ssh, gst_plugins_installed):
        """Test VIC as format converter in a transcode pipeline (encode -> decode -> VIC -> re-encode)."""
        spec = _conftest.get_hardware_spec(_conftest.HARDWARE_MODEL_NAME)
        if not spec.get("video_enc", {}).get("supported"):
            pytest.skip("Video encoder not supported on this hardware")
        ssh.sudo("rm -f /tmp/vic_transcode.mp4", fail_on_rc=False)
        result = ssh.sudo(
            "gst-launch-1.0 videotestsrc num-buffers=30 ! "
            "'video/x-raw, format=(string)NV12, width=(int)1280, height=(int)720' ! "
            "nvvidconv ! 'video/x-raw(memory:NVMM), format=(string)NV12' ! "
            "nvv4l2h264enc ! h264parse ! nvv4l2decoder ! "
            "nvvidconv ! 'video/x-raw(memory:NVMM), format=(string)RGBA' ! "
            "nvvidconv ! 'video/x-raw(memory:NVMM), format=(string)I420' ! "
            "nvv4l2h264enc ! h264parse ! qtmux ! "
            "filesink location=/tmp/vic_transcode.mp4 -e",
            fail_on_rc=False,
        )
        assert result.exit_status == 0, (
            f"VIC transcode pipeline failed: {result.stderr}"
        )
        verify = ssh.sudo("test -s /tmp/vic_transcode.mp4", fail_on_rc=False)
        assert verify.exit_status == 0, "VIC transcode produced empty or missing file"
        ssh.sudo("rm -f /tmp/vic_transcode.mp4", fail_on_rc=False)

    def test_vic_gray8_pipeline(self, ssh):
        """Test VIC GRAY8 format conversion chain (decode -> GRAY8 -> I420)."""
        result = ssh.sudo(
            "gst-launch-1.0 videotestsrc num-buffers=30 ! "
            "'video/x-raw, format=(string)NV12, width=(int)640, height=(int)480' ! "
            "nvvidconv ! 'video/x-raw(memory:NVMM), format=(string)NV12' ! "
            "nvvidconv ! 'video/x-raw(memory:NVMM), format=(string)GRAY8' ! "
            "nvvidconv ! 'video/x-raw(memory:NVMM), format=(string)I420' ! "
            "fakesink",
            fail_on_rc=False,
        )
        assert result.exit_status == 0, (
            f"VIC GRAY8 conversion chain failed: {result.stderr}"
        )

    # -------------------------------------------------------------------------
    # Display-dependent tests (require physical display — not available headless)
    # -------------------------------------------------------------------------

    # TODO: Enable when physical display is available for testing.
    # These tests require nv3dsink / nvdrmvideosink which need an active display.
    #
    # def test_vic_rotation_to_display(self, ssh):
    #     """Test VIC rotation with display output (nv3dsink)."""
    #     result = ssh.sudo(
    #         "gst-launch-1.0 videotestsrc num-buffers=30 ! "
    #         "'video/x-raw, format=(string)NV12, width=(int)1920, height=(int)1080' ! "
    #         "nvvidconv ! 'video/x-raw(memory:NVMM), format=(string)NV12' ! "
    #         "nvvidconv flip-method=1 ! "
    #         "'video/x-raw(memory:NVMM), format=(string)I420' ! "
    #         "nv3dsink -e",
    #         fail_on_rc=False,
    #     )
    #     assert result.exit_status == 0, f"VIC rotation to display failed: {result.stderr}"
    #
    # def test_vic_crop_to_display(self, ssh):
    #     """Test VIC crop with display output (nv3dsink)."""
    #     result = ssh.sudo(
    #         "gst-launch-1.0 videotestsrc num-buffers=30 ! "
    #         "'video/x-raw, format=(string)NV12, width=(int)1920, height=(int)1080' ! "
    #         "nvvidconv ! 'video/x-raw(memory:NVMM), format=(string)NV12' ! "
    #         "nvvidconv left=400 right=1520 top=200 bottom=880 ! "
    #         "'video/x-raw(memory:NVMM), format=(string)I420' ! "
    #         "nv3dsink -e",
    #         fail_on_rc=False,
    #     )
    #     assert result.exit_status == 0, f"VIC crop to display failed: {result.stderr}"
    #
    # def test_vic_scaling_to_drm_display(self, ssh):
    #     """Test VIC scaling with DRM display output (nvdrmvideosink)."""
    #     result = ssh.sudo(
    #         "gst-launch-1.0 videotestsrc num-buffers=30 ! "
    #         "'video/x-raw, format=(string)NV12, width=(int)1920, height=(int)1080' ! "
    #         "nvvidconv interpolation-method=1 ! "
    #         "'video/x-raw(memory:NVMM), format=(string)I420, width=1280, height=720' ! "
    #         "nvdrmvideosink -e",
    #         fail_on_rc=False,
    #     )
    #     assert result.exit_status == 0, f"VIC scaling to DRM display failed: {result.stderr}"
