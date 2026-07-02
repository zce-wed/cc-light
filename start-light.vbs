' cc-light launcher: start the traffic-light window silently (no console window)
Set sh = CreateObject("WScript.Shell")
home = sh.ExpandEnvironmentStrings("%USERPROFILE%")
sh.Run "pythonw """ & home & "\.claude\cc-light\cc_light.py""", 0, False
