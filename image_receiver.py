"""Bounded reassembly and decoding for the wearable's BLE JPEG stream."""

import io
import warnings

from PIL import Image


MAX_IMAGE_BYTES = 1024 * 1024
MAX_PACKET_BYTES = 493 + 2
MAX_IMAGE_DIMENSION = 2048
MAX_IMAGE_PIXELS = 2_000_000
END_MARKER = b"\xff\xff"


class InvalidImage(ValueError):
    """A completed BLE image is not a supported, bounded JPEG."""


class ImageReceiver:
    """Accept one ordered image at a time; packet zero starts or restarts it.

    The firmware pauses transfers during audio mode, so no transfer deadline is
    imposed. Retained state is bounded even if a transfer never completes.
    """

    def __init__(self):
        self.reset()

    def reset(self):
        self._buffer = bytearray()
        self._next_packet = None

    @property
    def buffered_bytes(self):
        return len(self._buffer)

    def feed(self, data):
        """Return detached image bytes on completion, or None otherwise.

        Malformed, out-of-order, or oversized input invalidates the entire
        current image. Recovery requires a fresh, valid packet zero.
        """
        if not isinstance(data, (bytes, bytearray)):
            self.reset()
            return None

        if data == END_MARKER:
            if self._next_packet is None:
                return None
            completed = bytes(self._buffer)
            self.reset()
            return completed

        if not 3 <= len(data) <= MAX_PACKET_BYTES:
            self.reset()
            return None

        packet_id = data[0] | (data[1] << 8)
        if packet_id == 0:
            self.reset()
            self._next_packet = 0

        if packet_id == 0xFFFF or packet_id != self._next_packet:
            self.reset()
            return None

        payload_size = len(data) - 2
        if payload_size > MAX_IMAGE_BYTES - len(self._buffer):
            self.reset()
            return None

        self._buffer.extend(data[2:])
        self._next_packet = packet_id + 1
        return None


def decode_jpeg(encoded_image):
    """Fully decode a bounded JPEG before inference, persistence, or display."""
    if not encoded_image or len(encoded_image) > MAX_IMAGE_BYTES:
        raise InvalidImage("Image exceeds the encoded size limit or is empty")
    if not encoded_image.endswith(b"\xff\xd9"):
        raise InvalidImage("JPEG is missing its end marker")

    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(encoded_image), formats=("JPEG",)) as image:
                if image.format != "JPEG":
                    raise InvalidImage("Only single JPEG images are supported")
                width, height = image.size
                if not (
                    0 < width <= MAX_IMAGE_DIMENSION
                    and 0 < height <= MAX_IMAGE_DIMENSION
                    and width * height <= MAX_IMAGE_PIXELS
                ):
                    raise InvalidImage("JPEG exceeds the decoded dimensions limit")
                image.load()
                return image.copy()
    except (
        OSError,
        ValueError,
        SyntaxError,
        OverflowError,
        Image.DecompressionBombError,
        Image.DecompressionBombWarning,
    ) as error:
        raise InvalidImage("Invalid or unsupported JPEG") from error
