import sys, glob
from PIL import Image, ImageDraw
VX, VY, PY, LANE = 497, 116, 326, 146
SX = 960/945
def box(off, y0, y1):
    f = ((y0 + y1) / 2 - VY) / (PY - VY); cx = 520 + off * LANE * f - (520 - VX) * (1 - f); w = 70 * f
    return [cx - w / 2, y0, w, y1 - y0]
out=[]
for p in sys.argv[2:]:
    im=Image.open(p).convert('RGB'); d=ImageDraw.Draw(im)
    for (y0,y1),col in (((225,275),'red'),((175,225),'yellow')):
        for o in (-1,0,1):
            x,y,w,h=box(o,y0,y1); d.rectangle([x*SX,y*SX,(x+w)*SX,(y+h)*SX],outline=col,width=2)
    out.append(im.crop((int(204*SX),int(17*SX),int(832*SX),int(370*SX))).resize((320,180)))
sheet=Image.new('RGB',(320*4,180*((len(out)+3)//4)))
for i,im in enumerate(out): sheet.paste(im,((i%4)*320,(i//4)*180))
sheet.save(sys.argv[1])
