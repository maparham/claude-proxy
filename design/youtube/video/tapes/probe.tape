Output out/probe.mp4
Set Shell "bash"
Set FontFamily "Menlo"
Set FontSize 28
Set Width 1920
Set Height 1080
Env HOME "/tmp/rahkar-demo/home"
Env PATH "/tmp/rahkar-demo/home/.local/bin:/usr/bin:/bin:/usr/sbin:/sbin:/opt/homebrew/bin"
Env BASH_SILENCE_DEPRECATION_WARNING "1"
Hide
Type "export PS1='~/project % '; cd ~/project; clear"
Enter
Show
Type "gclaude"
Enter
Sleep 10s
Screenshot out/probe1.png
