"""Compact price chart. Event spacing is ordinal, not an invented time scale."""
from io import BytesIO
from PIL import Image, ImageDraw, ImageFont


def render_price_chart(history: dict) -> bytes | None:
    events = history.get('events') or []
    if not events:
        return None
    values = [events[0]['old_price']] + [e['new_price'] for e in events]
    labels = ['До изменений'] + [str(e['at']) for e in events]
    image = Image.new('RGB', (1100, 620), '#f5f8f4')
    draw = ImageDraw.Draw(image)
    font = ImageFont.truetype('DejaVuSans.ttf', 23)
    small = ImageFont.truetype('DejaVuSans.ttf', 18)
    title = ImageFont.truetype('DejaVuSans.ttf', 35)
    draw.text((48, 30), 'Как менялась цена', font=title, fill='#18362f')
    draw.text((48, 82), 'млн ₸ · изменения по порядку, интервалы времени не в масштабе', font=small, fill='#587169')
    low, high = min(values), max(values)
    pad = max((high-low)*.18, 100000)
    low, high = low-pad, high+pad
    left, right, top, bottom = 135, 970, 155, 470
    for i in range(5):
        y = top+(bottom-top)*i/4
        value = high-(high-low)*i/4
        draw.line((left,y,right,y), fill='#dce6df', width=1)
        draw.text((25,y-12), f'{value/1e6:.2f}', font=small, fill='#587169')
    points = [(left+(right-left)*i/(len(values)-1), bottom-(v-low)/(high-low)*(bottom-top)) for i,v in enumerate(values)]
    for a,b in zip(points,points[1:]):
        draw.line([a,b],fill='#16815c' if b[1]>=a[1] else '#c27030',width=5)
    for x,y in points:
        draw.ellipse((x-5,y-5,x+5,y+5),fill='#18362f')
    indices = sorted({round(i*(len(values)-1)/min(4,len(values)-1)) for i in range(min(4,len(values)-1)+1)})
    for i in indices:
        x,y=points[i]
        draw.text((x, bottom+28),labels[i],font=small,fill='#587169',anchor='mt')
    for i in (0,len(values)-1):
        x,y=points[i]
        draw.text((x,y-33),f'{values[i]/1e6:g}',font=font,fill='#18362f',anchor='mt')
    delta=values[-1]-values[0]
    draw.text((48,565),f"За записанную историю: {delta/1e6:+g} млн ₸",font=font,fill='#18362f')
    out=BytesIO();image.save(out,format='PNG');return out.getvalue()
