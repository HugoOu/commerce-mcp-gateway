# Run from the repository root: vhs docs/demo-f14.tape
# Requires VHS (https://github.com/charmbracelet/vhs).
Output docs/demo-f14.gif

Set Shell "cmd"
Set FontSize 15
Set Width 1200
Set Height 950
Set Framerate 20
Set Theme "Dracula"
Set Padding 24

Sleep 1s
Type "uv run python scripts/demo.py --act f14 --fast"
Enter
Sleep 14s
