import sys
import numpy as np
from PIL import Image
boxes={'L':(395,95,75,28),'C':(470,95,85,28),'R':(555,95,75,28)}
def crop(a,b,S):
    x,y,w,h=b; return a[int(y*S):int((y+h)*S),int(x*S):int((x+w)*S)]
for p in sys.argv[1:]:
    im=Image.open(p).convert('RGB'); im=im.resize((240,int(im.height*240/im.width))); a=np.array(im).astype(int); S=240/945
    means={k:crop(a,b,S).reshape(-1,3).mean(0) for k,b in boxes.items()}
    med=np.median(np.stack(list(means.values())),0)
    print(p[-7:-4],' '.join(f'{k}={np.abs(m-med).sum():.0f}' for k,m in means.items()), ' means', ' '.join(str(m.astype(int).tolist()) for m in means.values()))
