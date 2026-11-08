Output out/term-install.mp4
Set Shell "bash"
Set FontFamily "Menlo"
Set FontSize 28
Set Width 1920
Set Height 1080
Set Padding 36
Set Margin 40
Set MarginFill "#faf6f3"
Set BorderRadius 18
Set WindowBar Colorful
Set Theme "Builtin Dark"
Set TypingSpeed 55ms
Set Framerate 30
Env HOME "/tmp/rahkar-demo/home"
Env PATH "/tmp/rahkar-demo/home/.local/bin:/usr/bin:/bin:/usr/sbin:/sbin:/opt/homebrew/bin"
Env BASH_SILENCE_DEPRECATION_WARNING "1"
Hide
Type "export PS1='~/project % '; cd ~/project; clear"
Enter
Sleep 500ms
Show
Sleep 800ms
Type "curl -fsSL https://dash.rahkar.pro/install | sh"
Sleep 600ms
Enter
Wait+Screen@900s /Next: .*gclaude/
Sleep 2.5s
