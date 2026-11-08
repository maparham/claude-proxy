Output out/v-prompt.mp4
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
Set TypingSpeed 45ms
Set Framerate 30
Env HOME "/private/tmp/rahkar-demo2/home"
Env TMPDIR "/tmp/rahkar-demo2/tmp/"
Env PATH "/private/tmp/rahkar-demo2/home/.local/bin:/usr/bin:/bin:/usr/sbin:/sbin:/opt/homebrew/bin"
Env BASH_SILENCE_DEPRECATION_WARNING "1"
Env DISABLE_AUTOUPDATER "1"
Env TERM "xterm-256color"
Env LANG "en_US.UTF-8"
Hide
Type "export PS1='$ '; cd ~/nan-e-sangak; clear"
Enter
Sleep 800ms
Show
Sleep 800ms
Type "gclaude"
Enter
Sleep 14s
Type "Build index.html in Persian (Farsi, right-to-left): a one-page landing page for a bakery called Nan-e Sangak, with a hero, a menu and a contact section."
Sleep 800ms
Enter
Sleep 120s
