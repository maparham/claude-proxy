Output out/term-prompt.mp4
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
Set TypingSpeed 45ms
Set Framerate 30
Env HOME "/tmp/rahkar-demo/home"
Env PATH "/tmp/rahkar-demo/home/.local/bin:/usr/bin:/bin:/usr/sbin:/sbin:/opt/homebrew/bin"
Env BASH_SILENCE_DEPRECATION_WARNING "1"
Env DISABLE_AUTOUPDATER "1"
Hide
Type "export PS1='~/project % '; cd ~/project; clear"
Enter
Sleep 800ms
Show
Sleep 800ms
Type "gclaude"
Enter
Sleep 8s
Type "Build index.html: a one-page landing page for a Persian bakery called Nan-e Sangak, with a hero, a menu and a contact section."
Sleep 800ms
Enter
Sleep 120s
