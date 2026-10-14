import sys
from PIL import Image
im=Image.open(sys.argv[1]).convert('RGB')
for spec in sys.argv[2:]:
    name,x,y=spec.split(':'); x,y=int(x),int(y)
    r=[im.getpixel((x+dx,y+dy)) for dx in (-3,0,3) for dy in (-3,0,3)]
    print(name,(x,y),tuple(sum(c[i] for c in r)//9 for i in range(3)))
