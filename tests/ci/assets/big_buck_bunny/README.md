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
  Times are presentation times (the film's first frame shows at 2/30 s), as the grids' samples are.

The first `edits.json` (2026-10-13) was never committed (`*.json` was ignored) and was lost with its container. It
was rebuilt on 2026-10-22 the same way: 143 candidates, each labelled by two independent passes over frame strips
(they agreed on all), the uncertain ones checked frame by frame. It has 125 cuts, as the first had. Its five gradual
changes are the title (3.65-4.85 s, from the first list's notes), two fades through black, the iris-out, and the
credits fading in from black; the first list's notes name a "post-credits fade" instead, which could not be matched
to the film. `window` is 3.0-498.5 s: after the opening fade from black, to the end of the credits' fade-in. Two whip
moves are ambiguous, where the first list had one: the tilt to the sky at 341.57 s and the pan onto the spikes at
449.33 s. Scored with the eyes as of this commit: precision 1.000, recall 0.938 (8 missed, none false).
