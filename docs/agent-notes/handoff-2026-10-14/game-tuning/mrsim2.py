import sys
import numpy as np
from PIL import Image
boxes={'L':(395,95,75,28),'C':(470,95,85,28),'R':(555,95,75,28)}
refs=[(350,235,50,30),(650,235,50,30)]
def crop(a,b,S):
    x,y,w,h=b; return a[int(y*S):int((y+h)*S),int(x*S):int((x+w)*S)]
for p in sys.argv[1:]:
    im=Image.open(p).convert('RGB'); im=im.resize((240,int(im.height*240/im.width))); a=np.array(im).astype(int); S=240/945
    ref=np.concatenate([crop(a,r,S).reshape(-1,3) for r in refs]).mean(0)
    out=[]
    for k,b in boxes.items():
        reg=crop(a,b,S); d=np.abs(reg-ref).sum(-1); out.append(f'{k}={(d>50).mean():.2f}')
    print(p[-7:-4],'ref',ref.astype(int),' '.join(out))
