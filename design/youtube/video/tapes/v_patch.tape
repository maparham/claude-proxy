Output out/v-patch.mp4
Set Shell "bash"
Set FontFamily "Menlo"
Set FontSize 28
Set Width 1080
Set Height 1920
Set Padding 24
Set Margin 28
Set MarginFill "#faf6f3"
Set BorderRadius 18
Set WindowBar Colorful
Set Theme "Builtin Dark"
Set TypingSpeed 55ms
Set Framerate 30
Env HOME "/tmp"
Env TMPDIR "/tmp/rahkar-demo2/tmp/"
Env PATH "/private/tmp/rahkar-demo2/home/.local/bin:/usr/bin:/bin:/usr/sbin:/sbin:/opt/homebrew/bin"
Env BASH_SILENCE_DEPRECATION_WARNING "1"
Env DISABLE_AUTOUPDATER "1"
Env TERM "xterm-256color"
Env LANG "en_US.UTF-8"
Env PS1 "$ "
Hide
Type "clear; printf '$ x\n'; for i in $(seq 21); do echo; done; printf 'Authorized as you@example.com.\n'"
Enter
Sleep 1s
Show
Sleep 600ms
