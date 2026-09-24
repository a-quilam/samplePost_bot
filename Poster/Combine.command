#!/bin/bash
# Собирает дерево проекта и весь *.py-код (без venv, кешей и .git) в combined_code.txt
# Двойной клик по этому файлу запускает Terminal и ждёт Enter.

set -euo pipefail

# перейти в папку, где лежит скрипт (важно при двойном клике)
cd "$(dirname "$0")"

OUT="combined_code.txt"

# шаблон исключений для find/tree
EXCL_DIRS='venv|.venv|env|__pycache__|.git|.mypy_cache|.pytest_cache|.idea|.vscode'

# функция: кастомное "дерево", если нет `tree`
fake_tree () {
  # печатаем только директории, с отступами по уровню
  # исключаем служебные каталоги
  find . \
    \( -path './venv' -o -path './.venv' -o -path './env' -o -path './__pycache__' -o -path './.git' -o -path './.mypy_cache' -o -path './.pytest_cache' -o -path './.idea' -o -path './.vscode' \) -prune -o \
    -type d -print \
  | LC_ALL=C sort \
  | sed '1d' \
  | awk -F/ '{indent=(NF-1)*2; printf "%*s%s\n", indent, "", $NF}'
}

# 1) Дерево проекта
{
  echo "===== TREE ====="
  if command -v tree >/dev/null 2>&1; then
    # есть tree (установить: brew install tree)
    tree -I "$EXCL_DIRS" -L 3
  else
    fake_tree
  fi

  echo
  echo "===== CODE ====="
} > "$OUT"

# 2) Весь Python-код проекта (в предсказуемом порядке)
#   - игнорируем venv/.git/кеши
#   - печатаем маркер «==== path ====» и содержимое
find . \
  \( -path './venv/*' -o -path './.venv/*' -o -path './env/*' -o -path './__pycache__/*' -o -path './.git/*' -o -path './.mypy_cache/*' -o -path './.pytest_cache/*' -o -path './.idea/*' -o -path './.vscode/*' \) -prune -o \
  -type f -name '*.py' -print0 \
| LC_ALL=C sort -z \
| while IFS= read -r -d '' f; do
    printf '==== %s ====\n' "$f" >> "$OUT"
    cat "$f" >> "$OUT"
    printf '\n' >> "$OUT"
  done

echo
echo "Готово: $OUT"
printf "Нажмите Enter для выхода..."
# читаем напрямую из TTY, чтобы окно не закрылось
read -r </dev/tty