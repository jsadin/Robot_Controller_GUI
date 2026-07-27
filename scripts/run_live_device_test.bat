@echo off
REM 真机三设备测试：必须用带 elite_cs_sdk 的 ES66 venv
set ROOT=%~dp0..
set VENV_PY=%ROOT%\ES66\ELITE_ROBOTS_ES66\elite_teleop_gui\.venv\Scripts\python.exe
set PYTHONPATH=%ROOT%

if not exist "%VENV_PY%" (
  echo Missing ES66 venv python: %VENV_PY%
  exit /b 1
)

echo Using: %VENV_PY%
echo Tip: set ROBOT_CAMERA_PASSWORD=your_hikvision_password
"%VENV_PY%" "%ROOT%\scripts\live_device_test.py"
pause
