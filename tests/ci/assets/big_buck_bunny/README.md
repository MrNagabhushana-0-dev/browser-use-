What the eyes see of *Big Buck Bunny* (c) 2008 Blender Foundation, www.bigbuckbunny.org, licensed CC BY 3.0
(https://creativecommons.org/licenses/by/3.0/). Derived from `bbb_sunflower_1080p_30fps_normal.mp4` (634.6 s, the
same edit as Wikimedia Commons' "Big Buck Bunny 4K.webm"), downloaded from download.blender.org.

- `grids_6fps.xz`: xz-compressed rows of 304 bytes, one per sample at 6 samples a second from 0 s (frame
  `int(t * 30)` of the 30 fps film): a 16x16 luma grid (256 bytes, row-major), then a 4x4 RGB grid (48 bytes).
  Made with ffmpeg area scaling (1920x1080 -> 320x180 -> 16x16), as compositor sight reduces a picture with
  Pillow's BOX filter. The 4x4 grid is the 16x16 one averaged in 4x4 blocks.
- `edits.json`: the film's edit list, in seconds. `cuts` are hard cuts; `gradual` are fades and an iris-out,
  as (start, end); `ambiguous` is a whip tilt that could be called either way; `window` is the span that is
  scored (the first black frames and the end are left out). Built from ffmpeg's scene scores, then every
  candidate (score 0.1 or more) and every slow-change span was checked by eye, frame by frame where unclear.
