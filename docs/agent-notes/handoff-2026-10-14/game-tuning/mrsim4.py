import sys, glob
import numpy as np
from PIL import Image
rows={'far':(92,18),'mid':(110,20)}
lanes={'L':(395,75),'C':(470,85),'R':(555,75)}
def crop(a,x,y,w,h,S): return a[int(y*S):int((y+h)*S),int(x*S):int((x+w)*S)]
ema={}
files=sorted(glob.glob(sys.argv[1]+'/*.png'))[int(sys.argv[2]):int(sys.argv[3])]
for p in files:
    im=Image.open(p).convert('RGB'); im=im.resize((240,int(im.height*240/im.width))); a=np.array(im).astype(int); S=240/945
    out=[]
    for r,(y,h) in rows.items():
        for k,(x,w) in lanes.items():
            m=crop(a,x,y,w,h,S).reshape(-1,3).mean(0); key=r+k
            e=ema.get(key,m); d=np.abs(m-e).sum(); ema[key]=0.7*e+0.3*m
            out.append(f'{key}={d:3.0f}')
    print(p[-7:-4],' '.join(out))
