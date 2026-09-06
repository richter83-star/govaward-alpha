@echo off
rem GovAward Alpha — Silent Dashboard Launcher
rem Starts dashboard in the background and opens default browser to http://127.0.0.1:8050/

cd /d "%~dp0"
start "" "C:\Users\17146\AppData\Local\Programs\Python\Python312\pythonw.exe" "%~dp0launch_dashboard.py"
