"""Build Hatuli's text-only social cards with the existing house/check mark."""
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "static" / "branding"
REGULAR = Path("C:/Windows/Fonts/segoeui.ttf")
BOLD = Path("C:/Windows/Fonts/seguisb.ttf")
INK, GREEN, PAPER, MUTED = "#183c35", "#236b54", "#f8f8f3", "#5f746b"


def font(size, bold=False):
    return ImageFont.truetype(str(BOLD if bold else REGULAR), size)


def wrapped(draw, text, xy, width, size, bold=False, color=INK, gap=12):
    face = font(size, bold)
    words, lines, line = text.split(), [], ""
    for word in words:
        candidate = f"{line} {word}".strip()
        if draw.textlength(candidate, font=face) > width and line:
            lines.append(line)
            line = word
        else:
            line = candidate
    lines.append(line)
    x, y = xy
    for line in lines:
        draw.text((x, y), line, font=face, fill=color)
        y += size + gap
    return y


def logo(draw, x, y, size=60):
    scale = size / 32
    def coords(points):
        return [(x + a * scale, y + b * scale) for a, b in points]
    draw.rounded_rectangle((x, y, x + size, y + size), radius=size / 5, fill="#e6eee7")
    draw.line(coords([(4, 15), (16, 5), (28, 15), (28, 27), (7, 27), (7, 15)]), fill=INK, width=4)
    draw.line(coords([(12, 19), (15, 22), (22, 14)]), fill=GREEN, width=4)
    draw.text((x + size + 18, y - 4), "Hatuli", font=font(size - 10, True), fill=INK)


def card(w, h, name):
    image = Image.new("RGB", (w, h), PAPER)
    draw = ImageDraw.Draw(image)
    if h < 800:
        logo(draw, 58, 42)
        draw.text((58, 132), "ДЛЯ ПОКУПАТЕЛЯ КВАРТИРЫ В АСТАНЕ", font=font(19, True), fill=GREEN)
        wrapped(draw, "Что проверить до задатка?", (58, 176), 620, 61, True)
        wrapped(draw, "История цены. Похожие предложения. Вопросы продавцу.", (58, 345), 570, 29, color=MUTED, gap=9)
        draw.rounded_rectangle((720, 126, 1142, 482), radius=24, fill="white", outline="#dce6dd", width=2)
        draw.text((750, 151), "ПРИМЕР РАЗБОРА", font=font(19, True), fill=MUTED)
        labels = [("01", "Как менялась цена"), ("02", "Что не сходится"), ("03", "Что спросить продавца")]
        for n, (number, text) in enumerate(labels):
            yy = 215 + n * 78
            draw.rounded_rectangle((750, yy, 800, yy + 45), radius=10, fill="#e9f0e9")
            draw.text((760, yy + 5), number, font=font(23, True), fill=GREEN)
            draw.text((818, yy + 6), text, font=font(22, True), fill=INK)
        draw.line((58, 512, 1142, 512), fill="#dce6dd", width=2)
        draw.text((58, 536), "Посмотрите открытый пример →", font=font(28, True), fill=GREEN)
        draw.text((58, 581), "hatuli.ai-groundtruth.com/example", font=font(20), fill=MUTED)
        draw.text((800, 553), "Свой разбор — по заявке", font=font(21), fill=MUTED)
    else:
        pad = 76
        logo(draw, pad, 70, 80)
        top = 190 if h < 1400 else 360
        draw.text((pad, top), "КВАРТИРА В АСТАНЕ", font=font(26, True), fill=GREEN)
        end = wrapped(draw, "Что проверить до задатка?", (pad, top + 64), w - 2 * pad, 76 if h < 1400 else 88, True, gap=13)
        end = wrapped(draw, "История цены и конкретные вопросы продавцу — в одном разборе.", (pad, end + 34), w - 2 * pad, 32 if h < 1400 else 36, color=MUTED, gap=12)
        yy = end + 65
        for number, label in [("01", "История цены"), ("02", "Расхождения в данных"), ("03", "Похожие предложения")]:
            draw.rounded_rectangle((pad, yy, w - pad, yy + 87), radius=18, fill="white", outline="#dce6dd", width=2)
            draw.text((pad + 22, yy + 19), number, font=font(31, True), fill=GREEN)
            draw.text((pad + 105, yy + 20), label, font=font(30, True), fill=INK)
            yy += 104
        footer = h - 225
        draw.line((pad, footer - 30, w - pad, footer - 30), fill="#dce6dd", width=2)
        draw.text((pad, footer), "Открытый пример на сайте", font=font(35, True), fill=GREEN)
        draw.text((pad, footer + 65), "hatuli.ai-groundtruth.com/example", font=font(27), fill=MUTED)
        draw.text((pad, footer + 122), "Свой разбор готовим вручную по заявке", font=font(23), fill=MUTED)
    OUT.mkdir(parents=True, exist_ok=True)
    image.save(OUT / name, optimize=True)
    print(f"{name}: {w}x{h}")


if __name__ == "__main__":
    card(1200, 630, "hatuli-share.png")
    card(1200, 1200, "hatuli-square.png")
    card(1080, 1920, "hatuli-story.png")
