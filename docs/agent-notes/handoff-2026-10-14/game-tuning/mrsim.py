import sys
import numpy as np
from PIL import Image
ROAD=[(165,95,85),(183,126,102),(195,147,138),(150,60,52),(175,110,95),(230,200,190)]
boxes={'L':(395,92,75,30),'C':(470,92,85,30),'R':(555,92,75,30)}
def frac(a,box,S):
    x,y,w,h=box
    reg=a[int(y*S):int((y+h)*S),int(x*S):int((x+w)*S)]
    near=np.zeros(reg.shape[:2],bool)
    for c in ROAD: near|=np.abs(reg-np.array(c)).sum(-1)<=45
    return 1-near.mean()
for p in sys.argv[1:]:
    im=Image.open(p).convert('RGB'); im=im.resize((240,int(im.height*240/im.width))); a=np.array(im).astype(int); S=240/945
    print(p[-7:-4],' '.join(f'{k}={frac(a,b,S):.2f}' for k,b in boxes.items()))
