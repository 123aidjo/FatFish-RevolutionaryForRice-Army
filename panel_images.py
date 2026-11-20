from io import BytesIO
from uuid import uuid4
import warnings

from PIL import Image, UnidentifiedImageError

from panel_config import atomic_write

MAX_BYTES = 4 * 1024 * 1024
MAX_PIXELS = 4_000_000
TYPES = {"image/png": "PNG", "image/jpeg": "JPEG", "image/webp": "WEBP"}


class InvalidImage(ValueError):
    pass


def normalize_image(content: bytes, media_type: str) -> bytes:
    if not content or len(content) > MAX_BYTES or media_type not in TYPES:
        raise InvalidImage
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(BytesIO(content), formats=[TYPES[media_type]]) as image:
                if image.width * image.height > MAX_PIXELS or getattr(image, "n_frames", 1) != 1:
                    raise InvalidImage
                image.verify()
            with Image.open(BytesIO(content), formats=[TYPES[media_type]]) as image:
                image.load()
                image.thumbnail((640, 640))
                rgba = image.convert("RGBA")
                rgba.info.clear()
                result = BytesIO()
                rgba.save(result, format="PNG")
                return result.getvalue()
    except (UnidentifiedImageError, OSError, SyntaxError, ValueError,
            Image.DecompressionBombError, Image.DecompressionBombWarning):
        raise InvalidImage from None


def import_image(store, directory, content: bytes, media_type: str) -> str:
    image = normalize_image(content, media_type)
    identifier = uuid4().hex
    path = directory / f"{identifier}.png"
    with store.transaction() as db:
        # 图片提交后才登记；异常时只清理此次导入的临时产物。
        atomic_write(path, image)
        try:
            db.execute("INSERT INTO panel_stickers (id, metadata) VALUES (?, '{}')", (identifier,))
            store.record_panel_action(db, "import_sticker")
        except Exception:
            path.unlink(missing_ok=True)
            raise
    return identifier
