# Desktop eye, first hands-on outside the browser (2026-11-15)

Recording: `desktop-eye.mp4` (the virtual display :97, 1280x900, 8 fps, about 4 minutes; made with `record_display`).

Setup: Xvfb with two apps and no window manager. A "notes" app (a Chromium `--app` window with WM_CLASS `EyeNotes`, a
local page with a text box and a Save button) granted in full, and an xterm granted at the click tier. The eye server ran
with `DISPLAY=:97 BROWSER_USE_DESKTOP_CONTROL=1 BROWSER_USE_DESKTOP_EYES=1 BROWSER_USE_DESKTOP_APPS="eyenotes,xterm:click"`.
Every action below was one `eye_desktop_*` tool call by the main agent. No verifier; one run.

What was tried and what the tools said:

1. `eye_desktop_look`: one 960x675 image of the whole screen, about 875 tokens. Both windows readable.
2. `eye_desktop_click` on the text box with `expect="Notes"`: "Clicked (220, 200) in eyenotes ... Keyboard focus: eyenotes."
3. `eye_desktop_type` of "the eye works outside the browser" with `expect="Notes"`: "Typed 33 characters in eyenotes".
4. `eye_desktop_click` on Save: "Clicked (45, 323) in eyenotes; the screen changed".
5. `eye_desktop_click` on the xterm: allowed (click tier), "Keyboard focus: xterm".
6. `eye_desktop_type` with the xterm focused: REFUSED, "typing is not allowed in "xterm": it is granted at the click tier",
   effect none. Nothing appeared in the xterm.
7. `eye_desktop_look` again: the text box reads "the eye works outside the browser".

Not checked: the page's "Saved: ..." line is below the window's visible part, so the Save click's result was not read back.
A click on an app that was not granted was not tried in this run.

Found:

- On the first try (the run before this one) the eye server's own browser opened a window over the whole display, and
  because it is not a granted app the look showed a grey screen labelled "hidden: chromium-browser (not granted)".
  Nothing was visible until that browser was stopped. In this run no browser tool was used, so it did not open.
- The first look in this run came 6 s after the notes app was started and showed only the xterm: the app's window was not
  up yet. A second look showed it. Not a defect of the eye.
