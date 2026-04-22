# assets/

Untracked source media — raw captures and masters. The optimized files that
actually ship live in `site/public/`. This directory is in
`fathomdx/.gitignore`.

## hero-video/

The sediment-fall loop that plays as the homepage hero background.

| File | Purpose |
|---|---|
| `mind-capture-source.webm` | Raw browser capture (VP9, variable timestamps, ~300 MB). |
| `mind-capture-source.mp4`  | Same frames, retimed to 120fps H.264 (~135 MB, 46s). |
| `mind-slow-183s.mp4`       | 30fps-playback slow-mo re-encode of the above (~135 MB, 183s). This is the master the shipped bg is derived from. |

The shipped file is `site/public/mind-bg.mp4`, encoded for ~8 MB. Regenerate
with ffmpeg when the source changes.
