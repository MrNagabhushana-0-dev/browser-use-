import sys
import numpy as np
from PIL import Image
RED=np.array((196,30,26)); S=240/945
for p in sys.argv[1:]:
    px=np.array(Image.open(p).convert('RGB').resize((240,136))).astype(int)
    bands=[]
    for y0 in range(100,280,10):
        reg=px[int(y0*S):int((y0+10)*S), int((512-30)*S):int((512+30)*S)]
        bands.append(int((np.abs(reg-RED).sum(-1)<=80).sum()))
    ext=sum(b>=2 for b in bands)
    print(p.split('/')[-1], 'ext', ext, bands)
