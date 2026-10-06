#!/usr/bin/env python3
"""build_brand_assets.py — 从品牌母图生成智账正式 icon 资产。

母图（唯一 icon master source，用户最终确认版）：
    web/assets/art/brand/zhizhang-master.png（RGBA，深蓝圆角底板 +
    打开的账本 + 蓝青数据柱 + 金色轨迹；圆角外自带透明边缘）

生成物（工程化适配，不改设计）：
    web/assets/art/brand/zhizhang-{1024,512,256,128,64,48,32,24,20,16}.png
    web/assets/art/brand/zhizhang.ico（256/128/64/48/32/24/20/16）
    src-tauri/icons/{32x32,128x128,128x128@2x,icon-64,icon}.png + icon.ico
        （Tauri bundle 要求的正式文件名，由同源资产覆盖）

适配原则：按不透明边界裁切 + 居中 + 统一留白；全程保留 RGBA 透明边缘；
小尺寸（≤32px）做温和锐化保证可识别性；不重新设计主体、不加文字、
不换构图。用法：.venv/Scripts/python scripts/build_brand_assets.py [master.png]
"""
import os
import sys

from PIL import Image, ImageFilter

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
BRAND_DIR = os.path.join(ROOT, 'web', 'assets', 'art', 'brand')
TAURI_ICONS = os.path.join(ROOT, 'src-tauri', 'icons')

SIZES = (1024, 512, 256, 128, 64, 48, 32, 24, 20, 16)
ICO_SIZES = (256, 128, 64, 48, 32, 24, 20, 16)
SHARPEN_BELOW = 33


def framed_square(im, margin=0.04):
    """按不透明边界裁切，再贴到带统一留白的正方形透明画布。"""
    bbox = im.getchannel('A').getbbox()
    if bbox:
        im = im.crop(bbox)
    w, h = im.size
    side = int(max(w, h) * (1 + 2 * margin))
    canvas = Image.new('RGBA', (side, side), (0, 0, 0, 0))
    canvas.paste(im, ((side - w) // 2, (side - h) // 2), im)
    return canvas


def render(im, size):
    out = im.resize((size, size), Image.LANCZOS)
    if size < SHARPEN_BELOW:
        out = out.filter(ImageFilter.UnsharpMask(radius=1.2, percent=60, threshold=2))
    return out


def write_ico(path, im):
    """multi-size ICO。以 256 帧为底（Pillow 以超大底图保存会缺 256 层）。"""
    base = render(im, 256)
    frames = [(s, render(im, s)) for s in ICO_SIZES[1:]]
    base.save(path, 'ICO',
              sizes=[(256, 256)] + [(s, s) for s, _ in frames],
              append_images=[f for _, f in frames])


def main():
    master = sys.argv[1] if len(sys.argv) > 1 else os.path.join(
        BRAND_DIR, 'zhizhang-master.png')
    if not os.path.isfile(master):
        print('master not found: %s' % master)
        return 1
    im = Image.open(master).convert('RGBA')
    im = framed_square(im)
    os.makedirs(BRAND_DIR, exist_ok=True)
    os.makedirs(TAURI_ICONS, exist_ok=True)

    # 正式 PNG 阶梯（含 Tray 工程化小尺寸 20/24/16）
    for size in SIZES:
        out = os.path.join(BRAND_DIR, 'zhizhang-%d.png' % size)
        render(im, size).save(out, 'PNG')
        print('png  %s' % os.path.relpath(out, ROOT))

    # Windows multi-size ICO（以 256 帧为底：Pillow 以超大底图保存会缺 256 层）
    ico_path = os.path.join(BRAND_DIR, 'zhizhang.ico')
    write_ico(ico_path, im)
    print('ico  %s' % os.path.relpath(ico_path, ROOT))

    # Tauri bundle 要求的正式文件名（同源资产覆盖）
    tauri_map = {
        '32x32.png': 32, '128x128.png': 128, '128x128@2x.png': 256,
        'icon-64.png': 64, 'icon.png': 512,
    }
    for name, size in tauri_map.items():
        out = os.path.join(TAURI_ICONS, name)
        render(im, size).save(out, 'PNG')
        print('tauri %s' % os.path.relpath(out, ROOT))
    write_ico(os.path.join(TAURI_ICONS, 'icon.ico'), im)
    print('tauri %s' % os.path.relpath(
        os.path.join(TAURI_ICONS, 'icon.ico'), ROOT))
    return 0


if __name__ == '__main__':
    sys.exit(main())
