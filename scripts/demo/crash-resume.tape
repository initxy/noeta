# vhs tape — records the crash-resume demo as the README GIF.
#
#   brew install vhs        # (pulls ttyd + ffmpeg)
#   vhs scripts/demo/crash-resume.tape
#
# Run from the repo root; writes docs/assets/crash-resume.gif.

Output docs/assets/crash-resume.gif

Set FontSize 15
Set Width 1150
Set Height 640
Set Padding 12
Set TypingSpeed 35ms

Type "uv run python examples/crash_resume.py"
Enter
Sleep 16s
