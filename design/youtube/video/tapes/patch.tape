Output out/patch.mp4
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
Env PS1 "$ "
Hide
Type "clear; printf '$ x\n'; for i in $(seq 20); do echo; done; printf 'Authorized as you@example.com.\n'"
Enter
Sleep 1s
Show
Sleep 500ms
Screenshot scenes/install_patch.png
