# Run from the repository root: vhs docs/demo.tape
# Requires VHS (https://github.com/charmbracelet/vhs).
Output docs/demo.gif

Set Shell "cmd"
Set FontSize 15
Set Width 1200
Set Height 900
Set Framerate 20
Set Theme "Dracula"
Set Padding 24

Sleep 1s
Type "uv run python scripts/demo.py --act transaction --fast"
Enter
Sleep 35s
