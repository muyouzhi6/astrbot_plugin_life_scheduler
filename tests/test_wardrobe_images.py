import base64
import tempfile
import unittest
from io import BytesIO
from pathlib import Path

from PIL import Image

from core.images import WardrobeImageStore


class WardrobeImageStoreTest(unittest.IsolatedAsyncioTestCase):
    async def test_same_entry_image_is_atomically_replaced(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first = root / "first.png"
            second = root / "second.png"
            Image.new("RGB", (1200, 1600), "red").save(first)
            Image.new("RGB", (600, 800), "blue").save(second)

            store = WardrobeImageStore(root / "wardrobe_images")
            target = await store.save("wardrobe-stable-id", str(first))
            first_uri = store.data_uri("wardrobe-stable-id")

            replaced_target = await store.save("wardrobe-stable-id", str(second))
            second_uri = store.data_uri("wardrobe-stable-id")

            self.assertEqual(target, replaced_target)
            self.assertNotEqual(first_uri, second_uri)
            self.assertEqual(len(list(store.directory.glob("*.jpg"))), 1)
            with Image.open(target) as image:
                self.assertLessEqual(image.width, 960)
                self.assertLessEqual(image.height, 1280)
                self.assertGreater(image.getpixel((10, 10))[2], 200)

            encoded = second_uri.removeprefix("data:image/jpeg;base64,")
            with Image.open(BytesIO(base64.b64decode(encoded))) as image:
                self.assertEqual(image.format, "JPEG")

    async def test_prune_removes_images_for_deleted_entries(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source.png"
            Image.new("RGB", (100, 100), "white").save(source)
            store = WardrobeImageStore(root / "wardrobe_images")

            await store.save("wardrobe-keep", str(source))
            await store.save("wardrobe-remove", str(source))
            store.prune({"wardrobe-keep"})

            self.assertTrue(store.exists("wardrobe-keep"))
            self.assertFalse(store.exists("wardrobe-remove"))
