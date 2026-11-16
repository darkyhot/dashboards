#!/usr/bin/env bash
# Создаёт папку новой витрины: marts/<витрина>/{intents,openspec/{specs,changes/archive},sql/{ddl,functions,migrations}}
# и симлинк на общий конфиг. Запуск из папки openspec/: scripts/new_mart.sh <витрина>
set -euo pipefail
cd "$(dirname "$0")/.."
name="${1:?укажи имя витрины: scripts/new_mart.sh <витрина>}"
dir="marts/$name"
[ -e "$dir" ] && { echo "уже есть: $dir"; exit 1; }
mkdir -p "$dir/intents" "$dir/openspec/specs" "$dir/openspec/changes/archive" \
         "$dir/sql/ddl" "$dir/sql/functions" "$dir/sql/migrations"
touch "$dir/intents/.gitkeep" "$dir/openspec/specs/.gitkeep" "$dir/openspec/changes/archive/.gitkeep" \
      "$dir/sql/ddl/.gitkeep" "$dir/sql/functions/.gitkeep" "$dir/sql/migrations/.gitkeep"
ln -s ../../../shared/config.yaml "$dir/openspec/config.yaml"
echo "создано: $dir"
