from pathlib import Path

from PIL import Image

SIZES = (16, 24, 32, 48, 64, 128, 256)
ROOT = Path(__file__).resolve().parent


def icon_canvas(source: Path) -> Image.Image:
    """从确认的透明插画导出带少量留边的图标画布。"""
    with Image.open(source) as original:
        image = original.convert("RGBA")
    alpha = image.getchannel("A")
    if alpha.getextrema() != (0, 255):
        raise ValueError("角色插画需要真实透明背景")
    bounds = alpha.point(lambda value: 255 if value >= 32 else 0).getbbox()
    cropped = image.crop(bounds)
    cropped.thumbnail((242, 242), Image.Resampling.LANCZOS)
    canvas = Image.new("RGBA", (256, 256))
    canvas.alpha_composite(cropped, ((256 - cropped.width) // 2, (256 - cropped.height) // 2))
    return canvas


def main() -> None:
    icon = icon_canvas(ROOT / "whale-girl.png")
    icon.save(ROOT / "fish-preview.png", format="PNG")
    icon.save(ROOT / "fish.ico", format="ICO", sizes=[(side, side) for side in SIZES])


if __name__ == "__main__":
    main()
