@echo off
setlocal
cd /d "%~dp0"

rem Change this if "python" points at the wrong install, e.g. set "PY=py -3.13"
set "PY=python"

echo Installing dependencies...
%PY% -m pip install -r requirements.txt pyinstaller || goto :error

echo Building exe...
%PY% -m PyInstaller --noconfirm --clean --onefile --noconsole ^
  --name "SMTC-Discord-RPC" ^
  --icon "icon.ico" ^
  --add-data "icon.ico;." ^
  --hidden-import pystray._win32 ^
  smtc_discord_rpc.py || goto :error

echo.
echo Done! Your exe is at: dist\SMTC-Discord-RPC.exe
pause
exit /b 0

:error
echo.
echo Build failed, check the messages above.
pause
exit /b 1
