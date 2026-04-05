@echo off
setlocal

cd /d "C:\Users\Dmytro Govor\Documents\Weather"

py -m pip install requests
py .\weather_history_html_truth_preview_monitor.py

pause