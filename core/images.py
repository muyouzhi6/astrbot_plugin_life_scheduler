import asyncio
import base64
import io
import re
from pathlib import Path
from urllib.parse import unquote, urlparse

import aiohttp
from PIL import Image, ImageOps


class WardrobeImageStore:
    """Persist compact display images for wardrobe entries."""

    _MAX_BYTES = 20 * 1024 * 1024
    _MAX_PIXELS = 60_000_000
    _THUMBNAIL_SIZE = (960, 1280)

    def __init__(self, directory: Path):
        self.directory = directory
        self.directory.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _safe_name(entry_id: str) -> str:
        return re.sub(r"[^A-Za-z0-9_-]", "_", str(entry_id or ""))

    def _path(self, entry_id: str) -> Path:
        safe_name = self._safe_name(entry_id)
        if not safe_name:
            raise ValueError("Invalid wardrobe entry ID")
        return self.directory / f"{safe_name}.jpg"

    async def save(self, entry_id: str, source: str) -> Path:
        """Normalize a local path or URL and atomically replace its thumbnail.

        Args:
            entry_id: Stable wardrobe entry ID.
            source: Local image path, file URL, or HTTP(S) URL.

        Returns:
            The persisted thumbnail path.

        Raises:
            ValueError: If the source is missing, too large, or not an image.
        """
        source_text = str(source or "").strip()
        if not source_text:
            raise ValueError("Image source is empty")

        parsed = urlparse(source_text)
        if parsed.scheme in {"http", "https"}:
            timeout = aiohttp.ClientTimeout(total=30)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(source_text) as response:
                    response.raise_for_status()
                    data = await response.read()
        else:
            source_path = (
                Path(unquote(parsed.path))
                if parsed.scheme == "file"
                else Path(source_text)
            )
            if not source_path.is_file():
                raise ValueError("Image file is unavailable")
            data = await asyncio.to_thread(source_path.read_bytes)

        if len(data) > self._MAX_BYTES:
            raise ValueError("Image is larger than 20 MB")

        target = self._path(entry_id)
        await asyncio.to_thread(self._write_thumbnail, data, target)
        return target

    def _write_thumbnail(self, data: bytes, target: Path) -> None:
        with Image.open(io.BytesIO(data)) as source:
            if source.width * source.height > self._MAX_PIXELS:
                raise ValueError("Image resolution is too large")
            source.load()
            image = ImageOps.exif_transpose(source).convert("RGBA")
            background = Image.new("RGBA", image.size, (255, 255, 255, 255))
            background.alpha_composite(image)
            image = background.convert("RGB")
            image.thumbnail(self._THUMBNAIL_SIZE, Image.Resampling.LANCZOS)

            tmp_path = target.with_suffix(".tmp")
            image.save(tmp_path, format="JPEG", quality=86, optimize=True)
            tmp_path.replace(target)

    def exists(self, entry_id: str) -> bool:
        """Return whether an entry currently has a display image."""
        return self._path(entry_id).is_file()

    def data_uri(self, entry_id: str) -> str:
        """Return a self-contained JPEG data URI for HTML rendering."""
        path = self._path(entry_id)
        if not path.is_file():
            return ""
        encoded = base64.b64encode(path.read_bytes()).decode("ascii")
        return f"data:image/jpeg;base64,{encoded}"

    def remove(self, entry_id: str) -> None:
        """Remove an entry image if one exists."""
        self._path(entry_id).unlink(missing_ok=True)

    def prune(self, valid_entry_ids: set[str]) -> None:
        """Remove images whose wardrobe entries were deleted in the panel."""
        valid_names = {self._safe_name(entry_id) for entry_id in valid_entry_ids}
        for path in self.directory.glob("*.jpg"):
            if path.stem not in valid_names:
                path.unlink(missing_ok=True)
