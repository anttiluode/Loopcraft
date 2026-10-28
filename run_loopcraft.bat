@echo off
setlocal
cd /d "%~dp0"
where py >nul 2>&1
if errorlevel 1 (
    echo Python launcher "py" not found.
    echo Install Python from https://www.python.org/downloads/windows/
    echo Make sure the launcher is enabled.
    pause
    exit /b 1
)
py -3 -c "import yt_dlp, PIL, imageio_ffmpeg" >nul 2>&1
if errorlevel 1 (
    echo Installing LoopCraft dependencies ...
    py -3 -m pip install --upgrade yt-dlp pillow imageio-ffmpeg
    if errorlevel 1 (
        echo Dependency installation failed.
        pause
        exit /b 1
    )
)
py -3 loopcraft.py
if errorlevel 1 pause
