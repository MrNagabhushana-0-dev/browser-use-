import sys, glob
import numpy as np
from PIL import Image
GROUND = [(172,147,96),(160,112,66),(205,130,30),(230,160,35),(187,123,45),(215,150,45),(205,190,115),(185,138,82),(220,161,41)]
TOL = 45
def box(o, y0, y1):
    y = (y0 + y1) / 2; s = 0.5 * (y - 80); cx = 512 + o * s; w = 0.5 * s
    return (cx - w / 2, y0, w, y1 - y0)
def ground(px, b, scale):
    x, y, w, h = b
    x0, y0, x1, y1 = int(x * scale), int(y * scale), max(int(x * scale) + 1, int((x + w) * scale)), max(int(y * scale) + 1, int((y + h) * scale))
    reg = px[y0:y1, x0:x1].astype(int)
    hit = np.zeros(reg.shape[:2], bool)
    for c in GROUND:
        hit |= np.abs(reg - np.array(c)).sum(-1) <= TOL
    return hit.sum(), hit.size
for p in sys.argv[1:]:
    im = Image.open(p).convert('RGB').resize((240, 136))
    px = np.array(im); scale = 240 / 945
    near = [ground(px, box(o, 225, 260), scale) for o in (-1, 0, 1)]
    far = [ground(px, box(o, 180, 220), scale) for o in (-1, 0, 1)]
    print(p.split('/')[-1], 'near', ' '.join(f'{a}/{b}' for a, b in near), ' far', ' '.join(f'{a}/{b}' for a, b in far))
