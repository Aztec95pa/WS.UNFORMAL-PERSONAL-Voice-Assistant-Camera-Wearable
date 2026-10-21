import ast
import datetime
import io
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest import mock

from PIL import Image, JpegImagePlugin

from image_receiver import (
    END_MARKER,
    MAX_IMAGE_BYTES,
    MAX_IMAGE_PIXELS,
    ImageReceiver,
    InvalidImage,
    decode_jpeg,
)


ROOT = Path(__file__).resolve().parents[1]
FIXTURE = (ROOT / "Pictures" / "3927640110.jpg").read_bytes()


def packet(index, payload=b"a"):
    return index.to_bytes(2, "little") + payload


def receive_image(receiver, encoded=FIXTURE):
    for index, offset in enumerate(range(0, len(encoded), 493)):
        result = receiver.feed(packet(index, encoded[offset:offset + 493]))
        if result is not None:
            raise AssertionError("Image completed before its terminator")
    return receiver.feed(END_MARKER)


def jpeg(size=(320, 240)):
    output = io.BytesIO()
    with Image.new("RGB", size, "green") as image:
        image.save(output, "JPEG")
    return output.getvalue()


class ImageReceiverTests(unittest.TestCase):
    def setUp(self):
        self.receiver = ImageReceiver()

    def assert_recovery(self):
        self.assertEqual(self.receiver.buffered_bytes, 0)
        self.assertIsNone(self.receiver.feed(packet(1)))
        self.assertIsNone(self.receiver.feed(END_MARKER))
        self.assertEqual(receive_image(self.receiver), FIXTURE)

    def test_firmware_fixture_and_multiple_images(self):
        for _ in range(2):
            self.assertEqual(receive_image(self.receiver), FIXTURE)
            self.assertEqual(self.receiver.buffered_bytes, 0)
            self.assertIsNone(self.receiver.feed(END_MARKER))

    def test_one_chunk_bytearray_and_exact_sized_final_chunk(self):
        self.receiver.feed(bytearray(packet(0, b"abc")))
        self.assertEqual(self.receiver.feed(bytearray(END_MARKER)), b"abc")
        data = b"x" * (493 * 2)
        self.assertEqual(receive_image(self.receiver, data), data)

    def test_valid_packet_zero_restarts_an_unfinished_image(self):
        self.receiver.feed(packet(0, b"old"))
        self.receiver.feed(packet(1, b"image"))
        self.assertEqual(receive_image(self.receiver), FIXTURE)

    def test_malformed_notification_invalidates_current_image(self):
        for malformed in (b"", b"\x00", b"\x00\x00", b"\x01\x00", packet(1, b"x" * 494), None, memoryview(b"abc")):
            with self.subTest(malformed=type(malformed).__name__):
                self.receiver.feed(packet(0))
                self.assertIsNone(self.receiver.feed(malformed))
                self.assert_recovery()

    def test_duplicate_gap_reordered_and_reserved_packet_invalidate(self):
        for bad_index in (1, 3, 0xFFFF):
            with self.subTest(index=bad_index):
                self.receiver.feed(packet(0))
                self.receiver.feed(packet(1))
                self.assertIsNone(self.receiver.feed(packet(bad_index)))
                self.assert_recovery()

    def test_orphan_packets_and_end_do_not_create_an_image(self):
        for data in (packet(1), packet(42), END_MARKER):
            self.assertIsNone(self.receiver.feed(data))
            self.assertEqual(self.receiver.buffered_bytes, 0)

    def test_exact_encoded_limit_is_accepted(self):
        encoded = b"x" * MAX_IMAGE_BYTES
        self.assertEqual(receive_image(self.receiver, encoded), encoded)

    def test_overflow_rejects_before_append_and_continued_stream_stays_empty(self):
        chunk = b"x" * 493
        for index in range(MAX_IMAGE_BYTES // 493):
            self.receiver.feed(packet(index, chunk))
            self.assertLessEqual(self.receiver.buffered_bytes, MAX_IMAGE_BYTES)
        next_index = MAX_IMAGE_BYTES // 493
        remaining = MAX_IMAGE_BYTES - self.receiver.buffered_bytes
        self.assertIsNone(self.receiver.feed(packet(next_index, b"x" * (remaining + 1))))
        for index in range(next_index + 1, next_index + 100):
            self.assertIsNone(self.receiver.feed(packet(index, chunk)))
            self.assertEqual(self.receiver.buffered_bytes, 0)
        self.assert_recovery()

    def test_uint16_sequence_cannot_wrap_or_reinterpret_end_as_payload(self):
        for index in range(0xFFFF):
            self.receiver.feed(packet(index))
        self.assertEqual(self.receiver.buffered_bytes, 0xFFFF)
        self.assertIsNone(self.receiver.feed(packet(0xFFFF)))
        self.assert_recovery()

    def test_disconnect_reset_drops_partial_image(self):
        self.receiver.feed(packet(0))
        self.receiver.reset()
        self.assert_recovery()

class DecodeJpegTests(unittest.TestCase):
    def test_real_fixture_is_fully_loaded_and_rotation_still_works(self):
        with decode_jpeg(FIXTURE) as image:
            self.assertEqual(image.size, (240, 320))
            self.assertEqual(image.rotate(-90, expand=True).size, (320, 240))
            image.getpixel((0, 0))

    def test_exact_pixel_limit_is_accepted(self):
        self.assertEqual(MAX_IMAGE_PIXELS, 2000 * 1000)
        with decode_jpeg(jpeg((2000, 1000))) as image:
            self.assertEqual(image.size, (2000, 1000))

    def test_invalid_truncated_and_non_jpeg_images_are_rejected(self):
        png = io.BytesIO()
        with Image.new("RGB", (1, 1)) as image:
            image.save(png, "PNG")
        for encoded in (b"", b"garbage\xff\xd9", FIXTURE[:-2], FIXTURE[:len(FIXTURE) // 2], png.getvalue() + b"\xff\xd9"):
            with self.subTest(length=len(encoded)):
                with self.assertRaises(InvalidImage):
                    decode_jpeg(encoded)

    def test_decoder_tolerated_early_end_marker_still_has_bounded_pixels(self):
        # Pillow/libjpeg may fill missing entropy data when an EOI is supplied.
        # This boundary guarantees bounded decoding, not JPEG quality checking.
        encoded = FIXTURE[:len(FIXTURE) // 2] + b"\xff\xd9"
        try:
            image = decode_jpeg(encoded)
        except InvalidImage:
            return
        with image:
            self.assertLessEqual(image.width * image.height, MAX_IMAGE_PIXELS)

    def test_multi_picture_jpeg_is_rejected_before_load(self):
        output = io.BytesIO()
        with Image.new("RGB", (1, 1), "red") as first:
            with Image.new("RGB", (1, 1), "blue") as second:
                first.save(output, "MPO", save_all=True, append_images=[second])
        with mock.patch.object(JpegImagePlugin.JpegImageFile, "load") as load:
            with self.assertRaises(InvalidImage):
                decode_jpeg(output.getvalue())
            load.assert_not_called()

    def test_encoded_limit_is_checked_before_open(self):
        with mock.patch("image_receiver.Image.open") as image_open:
            with self.assertRaises(InvalidImage):
                decode_jpeg(b"x" * (MAX_IMAGE_BYTES + 1))
            image_open.assert_not_called()

    def test_dimensions_and_pixels_are_checked_before_load(self):
        for size in ((2049, 1), (1, 2049), (2001, 1000)):
            with self.subTest(size=size):
                encoded = jpeg(size)
                with mock.patch.object(JpegImagePlugin.JpegImageFile, "load") as load:
                    with self.assertRaises(InvalidImage):
                        decode_jpeg(encoded)
                    load.assert_not_called()

    def test_decompression_bomb_header_is_rejected_before_load(self):
        encoded = bytearray(jpeg((1, 1)))
        start = encoded.index(b"\xff\xc0")
        encoded[start + 5:start + 9] = b"\xff\xff\xff\xff"
        with mock.patch.object(JpegImagePlugin.JpegImageFile, "load") as load:
            with self.assertRaises(InvalidImage):
                decode_jpeg(encoded)
            load.assert_not_called()

    def test_pillow_decompression_warning_is_rejected(self):
        with mock.patch.object(Image, "MAX_IMAGE_PIXELS", 50_000):
            with self.assertRaises(InvalidImage):
                decode_jpeg(FIXTURE)


class NotificationIntegrationTests(unittest.TestCase):
    def setUp(self):
        # Compile only the callback, avoiding model initialization and live APIs.
        source = ast.parse((ROOT / "main.py").read_text())
        callback = next(node for node in ast.walk(source) if isinstance(node, ast.FunctionDef) and node.name == "img_notification_handler")
        self.receiver = ImageReceiver()
        self.image = mock.Mock()
        self.image.rotate.return_value = self.image
        self.image.resize.return_value = self.image
        self.consumer = mock.Mock(return_value="description")
        self.decoder = mock.Mock(return_value=self.image)
        self.label = mock.Mock()
        self.vectorstore = mock.Mock()
        self.image_df = SimpleNamespace(loc={})
        namespace = {
            "image_receiver": self.receiver,
            "decode_jpeg": self.decoder,
            "InvalidImage": InvalidImage,
            "datetime": datetime,
            "io": io,
            "moondream_pipeline": self.consumer,
            "image_df": self.image_df,
            "vectorstore": self.vectorstore,
            "Document": lambda **kwargs: kwargs,
            "ImageTk": SimpleNamespace(PhotoImage=mock.Mock()),
            "tk": SimpleNamespace(Label=mock.Mock(return_value=self.label)),
            "img_frame": object(),
            "print": mock.Mock(),
        }
        # The callback uses len(image_df); keep this tiny stand-in local.
        namespace["len"] = lambda value: len(value.loc) if value is self.image_df else len(value)
        exec(compile(ast.Module(body=[callback], type_ignores=[]), "<image-notification-callback>", "exec"), namespace)
        self.receive = namespace["img_notification_handler"]

    def send(self, encoded=FIXTURE):
        for index, offset in enumerate(range(0, len(encoded), 493)):
            self.receive(None, packet(index, encoded[offset:offset + 493]))
        self.receive(None, END_MARKER)

    def test_valid_callback_preserves_original_model_bytes_and_display(self):
        self.send()
        self.decoder.assert_called_once_with(FIXTURE)
        self.assertEqual(self.consumer.call_args.args[0].getvalue(), FIXTURE)
        self.image.rotate.assert_called_once_with(-90, expand=True)
        self.image.save.assert_called_once()
        self.vectorstore.add_documents.assert_called_once()
        self.label.grid.assert_called_once_with(row=0, column=1)
        self.assertEqual(len(self.image_df.loc), 1)

    def test_invalid_decoding_has_no_consumer_or_persistence_side_effects(self):
        self.decoder.side_effect = decode_jpeg
        self.send(b"not a JPEG")
        self.consumer.assert_not_called()
        self.image.save.assert_not_called()
        self.vectorstore.add_documents.assert_not_called()
        self.label.grid.assert_not_called()
        self.assertEqual(len(self.image_df.loc), 0)
        self.assertEqual(self.receiver.buffered_bytes, 0)

    def test_failed_consumer_cannot_be_retried_by_repeated_terminator(self):
        self.consumer.side_effect = RuntimeError("consumer failed")
        with self.assertRaises(RuntimeError):
            self.send()
        self.assertEqual(self.receiver.buffered_bytes, 0)
        self.receive(None, END_MARKER)
        self.consumer.assert_called_once()
        self.consumer.side_effect = None
        self.send()
        self.assertEqual(self.consumer.call_count, 2)
        self.assertEqual(len(self.image_df.loc), 1)

    def test_bad_sequence_never_reaches_decoder(self):
        self.receive(None, packet(0))
        self.receive(None, packet(2))
        self.receive(None, END_MARKER)
        self.decoder.assert_not_called()
        self.consumer.assert_not_called()


class ConnectionLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_disconnect_clears_connection_local_assembly_and_exits_loop(self):
        source = ast.parse((ROOT / "main.py").read_text())
        connect = next(node for node in ast.walk(source) if isinstance(node, ast.AsyncFunctionDef) and node.name == "connect_to_device")
        receiver = ImageReceiver()
        clients = []

        class FakeClient:
            def __init__(self, address, disconnected_callback):
                self.is_connected = True
                self.disconnected_callback = disconnected_callback
                self.notifications = {}
                clients.append(self)

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return False

            async def start_notify(self, uuid, callback):
                self.notifications[uuid] = callback

        sleep_calls = 0

        async def disconnect_after_subscription(_):
            nonlocal sleep_calls
            sleep_calls += 1
            self.assertEqual(sleep_calls, 1, "Connection loop continued after disconnect")
            client = clients[0]
            client.notifications["photo"](None, packet(0, b"partial image"))
            self.assertGreater(receiver.buffered_bytes, 0)
            client.is_connected = False
            client.disconnected_callback(client)
            self.assertEqual(receiver.buffered_bytes, 0)

        namespace = {
            "ImageReceiver": lambda: receiver,
            "BleakClient": FakeClient,
            "Label": mock.Mock(),
            "tk": SimpleNamespace(Frame=mock.Mock()),
            "PHOTO_UUID": "photo",
            "AUDIO_UUID": "audio",
            "COMMAND_UUID": "command",
            "asyncio": SimpleNamespace(sleep=disconnect_after_subscription),
            "print": mock.Mock(),
        }
        exec(compile(ast.Module(body=[connect], type_ignores=[]), "<ble-connect-method>", "exec"), namespace)
        app = SimpleNamespace(master=object(), connect_button=mock.Mock())
        await namespace["connect_to_device"](app, "offline-test-device")
        self.assertEqual(receiver.buffered_bytes, 0)
        self.assertEqual(sleep_calls, 1)


if __name__ == "__main__":
    unittest.main()
