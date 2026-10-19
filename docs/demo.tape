# Run from the repository root: vhs docs/demo.tape
# Requires VHS (https://github.com/charmbracelet/vhs).
Output docs/demo.gif

Set Shell "powershell"
Set FontSize 16
Set Width 1200
Set Height 700
Set Framerate 30
Set Theme "Dracula"
Set Padding 24

Type "uv run python scripts/demo.py --act transaction --fast"
Enter
Sleep 22s
