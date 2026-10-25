@echo off
title ETS2 Tuner (Beta)
cd /d "%~dp0"
where py >nul 2>nul
if not errorlevel 1 goto usepy
where python >nul 2>nul
if not errorlevel 1 goto usepython
echo Python 3 wurde nicht gefunden / Python 3 not found.
echo https://www.python.org/downloads/  ("Add python.exe to PATH")
goto ende
:usepy
py -3 ets2_tuner.py
goto ende
:usepython
python ets2_tuner.py
:ende
pause
