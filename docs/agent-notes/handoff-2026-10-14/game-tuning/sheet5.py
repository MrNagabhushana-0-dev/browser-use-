import sys
from PIL import Image, ImageDraw
out, d, a, b, step = sys.argv[1], sys.argv[2], int(sys.argv[3]), int(sys.argv[4]), int(sys.argv[5])
idx = list(range(a, b + 1, step)); cols = 6
S = Image.open(f'{d}/{a:03d}.png').width / 945
sheet = Image.new('RGB', (240 * cols, 135 * ((len(idx) + cols - 1) // cols)))
for k, i in enumerate(idx):
    im = Image.open(f'{d}/{i:03d}.png').convert('RGB')
    ImageDraw.Draw(im).rectangle([488*S, 108*S, 568*S, 136*S], outline='cyan', width=2)
    im = im.crop((int(209*S), int(16*S), int(850*S), int(376*S))).resize((240, 135))
    ImageDraw.Draw(im).text((4, 120), str(i), fill='yellow')
    sheet.paste(im, ((k % cols) * 240, (k // cols) * 135))
sheet.save(out)
