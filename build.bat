@echo off
REM Build AdCleaner.exe (BUILD_PLAN Phase 5).
REM Produces ONE standalone Windows exe, with Google's ADB tools bundled inside,
REM so the person using it needs no setup and no internet.

echo Installing PyInstaller (one time)...
python -m pip install --upgrade pyinstaller || goto :error

if exist platform-tools\adb.exe goto :haveadb
echo.
echo Downloading Google ADB tools to bundle into the app...
powershell -NoProfile -Command "Invoke-WebRequest -Uri 'https://dl.google.com/android/repository/platform-tools-latest-windows.zip' -OutFile 'pt.zip' -UseBasicParsing; Expand-Archive -Path 'pt.zip' -DestinationPath '.' -Force; Remove-Item 'pt.zip'" || goto :error
:haveadb

if exist scrcpy\scrcpy.exe goto :havescrcpy
echo.
echo Downloading scrcpy (screen mirroring) to bundle into the app...
powershell -NoProfile -Command "Invoke-WebRequest -Uri 'https://github.com/Genymobile/scrcpy/releases/download/v4.1/scrcpy-win64-v4.1.zip' -OutFile 'sc.zip' -UseBasicParsing; Expand-Archive -Path 'sc.zip' -DestinationPath '.' -Force; Rename-Item 'scrcpy-win64-v4.1' 'scrcpy'; Remove-Item 'sc.zip'" || goto :error
:havescrcpy

echo.
echo Building AdCleaner.exe ...
pyinstaller --onefile --windowed --name AdCleaner --add-data "platform-tools;platform-tools" --add-data "scrcpy;scrcpy" main.py || goto :error

echo.
echo ============================================================
echo  Done.  Your program is here:  dist\AdCleaner.exe
echo  Just send that one file to anyone - no install, no internet.
echo ============================================================
pause
exit /b 0

:error
echo.
echo Build failed. Make sure Python 3.11+ is installed and on PATH.
pause
exit /b 1
