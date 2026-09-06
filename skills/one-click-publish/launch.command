#!/bin/sh
set -eu
publish_package_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
printf '请输入素材存储文件夹的绝对路径（独立于技能包）：\n'
IFS= read -r publish_workspace
case "$publish_workspace" in
  /*) ;;
  *) printf '需要绝对路径，未启动。\n'; exit 1 ;;
esac
printf '请输入平台配置 JSON 的绝对路径；回车复用素材目录内的 config.local.json（没有则仅收素材）：\n'
IFS= read -r publish_config
if [ -n "$publish_config" ]; then
  exec python3 "$publish_package_dir/scripts/run_dashboard.py" --workspace "$publish_workspace" --config "$publish_config"
else
  exec python3 "$publish_package_dir/scripts/run_dashboard.py" --workspace "$publish_workspace"
fi
