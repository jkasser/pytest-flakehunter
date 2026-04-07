@echo off
:: Run from anywhere — paths are always relative to the project root
cd /d "%~dp0.."

echo Cleaning old build artifacts...
if exist dist rmdir /s /q dist
if exist build rmdir /s /q build

echo Building...
uv build
if errorlevel 1 exit /b 1

:: echo Uploading to TestPyPI...
python -m twine upload --repository testpypi dist/*

