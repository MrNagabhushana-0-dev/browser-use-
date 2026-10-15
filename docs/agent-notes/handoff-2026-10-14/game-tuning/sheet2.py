import sys
from PIL import Image, ImageDraw
out, d, a, b, step = sys.argv[1], sys.argv[2], int(sys.argv[3]), int(sys.argv[4]), int(sys.argv[5])
idx = list(range(a, b + 1, step)); cols = 6
first = Image.open(f'{d}/{a:03d}.png'); W = first.width; S = W / 945
sheet = Image.new('RGB', (240 * cols, 135 * ((len(idx) + cols - 1) // cols)))
for k, i in enumerate(idx):
    im = Image.open(f'{d}/{i:03d}.png').convert('RGB').crop((int(209*S), int(16*S), int(850*S), int(377*S))).resize((240, 135))
    ImageDraw.Draw(im).text((4, 120), str(i), fill='yellow')
    sheet.paste(im, ((k % cols) * 240, (k // cols) * 135))
sheet.save(out)
