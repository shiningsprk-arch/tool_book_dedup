# -*- coding: utf-8 -*-
"""生成工具图标 icon.png（程序化绘制，256×256）。

图形语义：**两本重复的书并成一本** —— 左边两本半透明的小书（重复项）向右汇入一本实心大书
（保留项），中间一道向下收拢的箭头。这是这个工具唯一要表达的事。

与 `tool_quality_check/scripts/make_icon.py` 同一套做法：按 1024 逻辑尺寸画好，再
LANCZOS 降采样到 256，最后量化到 256 色（图形是平色 + 渐变，量化后无可见色带，
而体积比 1024 直出小得多——虽然进 zip 后差异很小，见 README 里的实测说明）。

用法：python scripts/make_icon.py
"""
import os

from PIL import Image, ImageDraw

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.abspath(os.path.join(HERE, '..', 'icon.png'))

LOGICAL = 1024      # 逻辑画布
SIZE = 256          # 最终尺寸

# 配色：深蓝底 + 青色高光，与"书"的意象协调，深浅两套主题下都看得清
BG_TOP = (32, 58, 110)
BG_BOTTOM = (18, 34, 72)
BOOK_FRONT = (240, 246, 255)
BOOK_PAGE = (206, 224, 245)
DUPLICATE = (240, 246, 255, 130)   # 半透明：表示"重复项，将被并入"
ACCENT = (86, 204, 214)
ARROW = (120, 214, 222)


def rounded_rect(draw, box, radius, fill):
    draw.rounded_rectangle(box, radius=radius, fill=fill)


def draw_book(draw, x, y, width, height, fill, page_fill, radius=None):
    """一本书：封面 + 书脊高光。"""
    radius = radius if radius is not None else width * 0.10
    rounded_rect(draw, (x, y, x + width, y + height), radius, fill)
    # 书脊（左侧竖条）
    spine_w = width * 0.16
    rounded_rect(draw, (x, y, x + spine_w, y + height), radius * 0.8, page_fill)


def build():
    image = Image.new('RGBA', (LOGICAL, LOGICAL), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image, 'RGBA')

    # 背景：垂直渐变
    for index in range(LOGICAL):
        ratio = index / float(LOGICAL - 1)
        color = tuple(
            int(BG_TOP[i] + (BG_BOTTOM[i] - BG_TOP[i]) * ratio) for i in range(3))
        draw.line([(0, index), (LOGICAL, index)], fill=color + (255,))
    # 圆角遮罩（宿主图标是方图，圆角只作视觉收敛）
    mask = Image.new('L', (LOGICAL, LOGICAL), 0)
    ImageDraw.Draw(mask).rounded_rectangle(
        (0, 0, LOGICAL - 1, LOGICAL - 1), radius=LOGICAL * 0.20, fill=255)
    image.putalpha(mask)
    draw = ImageDraw.Draw(image, 'RGBA')

    # 两本重复的书（左侧，**明显错开**才看得出是两本；靠后那本更淡）
    dup_w, dup_h = LOGICAL * 0.21, LOGICAL * 0.32
    draw_book(draw, LOGICAL * 0.10, LOGICAL * 0.26, dup_w, dup_h,
              (240, 246, 255, 70), (206, 224, 245, 60))
    draw_book(draw, LOGICAL * 0.20, LOGICAL * 0.44, dup_w, dup_h,
              (240, 246, 255, 165), (206, 224, 245, 140))

    # 汇入符号：两本小书之间向中间收拢的箭头（"合并"的标准意象，不抢主体）
    cx = LOGICAL * 0.50
    cy = LOGICAL * 0.50
    draw.line([(LOGICAL * 0.37, cy), (LOGICAL * 0.47, cy)],
              fill=ARROW + (255,), width=int(LOGICAL * 0.035))
    draw.line([(LOGICAL * 0.53, cy), (LOGICAL * 0.63, cy)],
              fill=ARROW + (255,), width=int(LOGICAL * 0.035))
    draw.polygon([
        (cx - LOGICAL * 0.055, cy - LOGICAL * 0.075),
        (cx - LOGICAL * 0.055, cy + LOGICAL * 0.075),
        (cx + LOGICAL * 0.035, cy),
    ], fill=ARROW + (255,))

    # 保留的那一本（右侧，实心、更大）
    keep_x = LOGICAL * 0.58
    keep_y = LOGICAL * 0.24
    keep_w = LOGICAL * 0.30
    keep_h = LOGICAL * 0.52
    draw_book(draw, keep_x, keep_y, keep_w, keep_h, BOOK_FRONT, BOOK_PAGE)
    # 书页横线（"内容"的暗示），三条
    for index in range(3):
        line_y = keep_y + keep_h * (0.28 + index * 0.17)
        draw.line(
            [(keep_x + keep_w * 0.30, line_y),
             (keep_x + keep_w * 0.80, line_y)],
            fill=ACCENT + (235,), width=int(LOGICAL * 0.022))

    # 降采样 → 量化
    final = image.resize((SIZE, SIZE), Image.LANCZOS)
    quantized = final.convert('RGB').quantize(colors=256, method=Image.MEDIANCUT)
    quantized.save(OUT, 'PNG', optimize=True)
    return OUT


def main():
    path = build()
    size = os.path.getsize(path)
    with Image.open(path) as image:
        print('wrote %s' % path)
        print('  size=%s mode=%s bytes=%d' % (image.size, image.mode, size))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())