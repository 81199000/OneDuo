"""生成一键装图标：红→橙渐变圆角方块 + 白色向下箭头 + 托盘（原创，不借用其他 App 图标）"""
import os
from PIL import Image, ImageDraw

S = 1024
img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
bg = Image.new("RGBA", (S, S))
top, bot = (255, 59, 48), (255, 149, 0)
dr = ImageDraw.Draw(bg)
for y in range(S):
    t = y / (S - 1)
    dr.line([(0, y), (S, y)], fill=tuple(int(top[i] + (bot[i] - top[i]) * t) for i in range(3)) + (255,))
mask = Image.new("L", (S, S), 0)
ImageDraw.Draw(mask).rounded_rectangle((100, 100, S - 100, S - 100), radius=185, fill=255)
img.paste(bg, (0, 0), mask)
d = ImageDraw.Draw(img)
W = (255, 255, 255, 255)
cx = S // 2
d.rounded_rectangle((cx - 46, 300, cx + 46, 590), radius=40, fill=W)
d.polygon([(cx - 180, 500), (cx + 180, 500), (cx, 690)], fill=W)
d.rounded_rectangle((cx - 230, 720, cx + 230, 790), radius=35, fill=W)
img.save("icon-1024.png")
os.makedirs("OneTap.iconset", exist_ok=True)
for s in (16, 32, 128, 256, 512):
    img.resize((s, s), Image.LANCZOS).save(f"OneTap.iconset/icon_{s}x{s}.png")
    img.resize((s * 2, s * 2), Image.LANCZOS).save(f"OneTap.iconset/icon_{s}x{s}@2x.png")
