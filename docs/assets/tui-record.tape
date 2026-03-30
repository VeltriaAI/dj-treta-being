# VHS tape file for recording DJClaw TUI demo
Output docs/assets/tui-demo.gif
Set Theme "Catppuccin Mocha"
Set FontFamily "JetBrains Mono"
Set FontSize 14
Set Width 1400
Set Height 800
Set Padding 20
Set TypingSpeed 50ms

# Start TUI
Type "cd ~/beings/dj-treta && .venv/bin/djtreta tui"
Enter
Sleep 8s

# Let the TUI render with live data
Sleep 15s

# Type a command to show interaction
Type "/set"
Enter
Sleep 5s

Type "/stats"
Enter
Sleep 5s

# Let it run showing live updates
Sleep 10s
