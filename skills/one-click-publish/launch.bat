@echo off
setlocal
chcp 65001 >nul
set /p "publish_workspace=请输入素材存储文件夹的绝对路径："
if not defined publish_workspace exit /b 1
set /p "publish_config=请输入平台配置 JSON 的绝对路径；回车复用素材目录内 config.local.json（没有则仅收素材）："
if defined publish_config (
  python "%~dp0scripts\run_dashboard.py" --workspace "%publish_workspace%" --config "%publish_config%"
) else (
  python "%~dp0scripts\run_dashboard.py" --workspace "%publish_workspace%"
)
pause
