# Create a Google Slides presentation with content slides

Build a title slide and bullet slides in a new Google Slides deck via `gws`.

## Steps
1. Plan the slides: each slide is a title plus short bullet points. Keep
   each bullet to one line.
2. Run `execute_shell("python3 skills/_slides_create.py --title \"TITLE\" --slide \"Slide 1 Title|bullet 1|bullet 2\" --slide \"Slide 2 Title|bullet 1\"")`.
   Add one `--slide` per slide. Each value is pipe-separated: the first part
   is the slide title, the rest are bullets.
3. Reply with the presentation URL from the script output (the line starting
   with `https://docs.google.com/presentation/`).

## Rules
- Do not put `|` inside a bullet; it splits the bullet.
- On an error, report the message as it is; do not retry with different
  quoting.
