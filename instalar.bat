@echo off
rem Instalador para Windows: busca Python 3 y lanza instalar.py.
rem Nicotine+ debe estar CERRADO: al salir reescribe su config y pisaria los cambios.
rem
rem   instalar.bat                 (doble clic, o desde una terminal)
rem   instalar.bat --simular       ensena que haria
rem   instalar.bat --quitar        desinstala
rem   instalar.bat --help          todas las opciones
setlocal
set "AQUI=%~dp0"
set "CODIGO=1"

where py >nul 2>nul
if %errorlevel%==0 (
  py -3 "%AQUI%instalar.py" %*
  set "CODIGO=%errorlevel%"
  goto :fin
)

python -c "import sys" >nul 2>nul
if %errorlevel%==0 (
  python "%AQUI%instalar.py" %*
  set "CODIGO=%errorlevel%"
  goto :fin
)

echo Hace falta Python 3.8 o mas nuevo y no lo encuentro.
echo   Descargalo de https://www.python.org/downloads/windows/
echo   y al instalarlo marca "Add python.exe to PATH".

:fin
rem Si se abrio con doble clic, que la ventana no se cierre sola.
echo %cmdcmdline% | find /i "%~0" >nul && pause
exit /b %CODIGO%
