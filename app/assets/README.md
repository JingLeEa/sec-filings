# Homepage introduction

`sec-filings-intro.mp4` is the approved 20-second project animation, displayed at the top of `app/Home.py`. It autoplays muted and loops as a passive animation. Playback controls, mouse interaction and keyboard focus are disabled, so hovering or clicking does not reveal video actions. The app embeds the bundled file, so playback needs no external video host or API key.

The player is centered and capped at 640 pixels wide (360 pixels tall at its
16:9 aspect ratio). It shrinks to fit narrower screens.

`home_intro.js` initializes muted playback before loading the source and retries
when the player becomes ready or visible. This handles first-visit playback
without navigating away and back. It runs through `st.html` with JavaScript
enabled for these bundled assets only (Streamlit 1.52+). The player remains
outside keyboard focus, has no controls, and blocks its media context menu.
Explicit browser autoplay restrictions are respected.

The animation introduces disclosure changes, then peer comparison, then the chatbot. The peer comparison is an illustration; the chatbot uses a saved result from the app. Numbered “Feature” labels are omitted.

To update the animation, replace this file with a browser-compatible H.264 MP4 using the same filename. The current export is 1920 × 1080 at 30 fps, approximately 1.8 MB. No video-generation tools are needed to run or deploy the app.

## Application logo

`f2f-logo.png` is the supplied navy/cyan F2F mark and wordmark prepared as a
compact horizontal PNG (2172 × 724). Its white background keeps the navy artwork
visible on both light and dark sidebars. The shared `components.setup()` renders
it through `st.logo` above the sidebar navigation on every main dashboard page.
The sidebar displays it at 64 pixels high with balanced 20-pixel padding above
and below, and the collapsed header uses Streamlit's smaller native logo size.
The homepage starts with the introduction video.
The logo asset is bundled locally and requires no external image host or API at runtime.

Preparation used the built-in `image_gen` tool with the user-supplied image as
the edit target, with this prompt:

```text
Use case: precise-object-edit. Input image 1 is the edit target: the supplied F2F brand logo. Prepare this exact supplied logo as a compact horizontal PNG asset for a website sidebar header. Change only framing: remove the large empty white margins and leave a small consistent white padding around the complete existing artwork. Preserve the supplied design as closely as possible: identical connected italic F2F monogram at left, dark navy outer F shapes, bright cyan central 2 shape, and separate dark navy text 'F2F' to the right, unchanged lettering, proportions, spacing and colours. Keep the white background and crisp edges. Preserve both the left monogram and right wordmark together. Do not redesign, reinterpret, add any text, add shadows, or create a new logo. Output a tightly framed wide PNG.
```
