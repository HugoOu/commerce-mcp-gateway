# Run from the repository root: vhs docs/demo-f14.tape
# Requires VHS (https://github.com/charmbracelet/vhs).
Output docs/demo-f14.gif

Set Shell "powershell"
Set FontSize 16
Set Width 1200
Set Height 900
Set Framerate 30
Set Theme "Dracula"
Set Padding 24

Type "uv run python scripts/demo.py --act f14 --fast"
Enter
Sleep 10s
