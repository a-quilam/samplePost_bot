---
description: "Разработчик Poster: реализует согласованные изменения, пишет тесты, запускает целевые проверки"
mode: subagent
model: routerai/deepseek/deepseek-v4.1-flash-1017
color: "#2fbf71"
steps: 80
permissions:

  # --- Делегирование и вопросы ---
  - action: subagent
    resource: '*'
    effect: deny
  - action: question
    resource: '*'
    effect: deny

  # --- Правки файлов ---
  - action: edit
    resource: '*'
    effect: deny
  - action: edit
    resource: 'Poster/*.py'
    effect: allow
  - action: edit
    resource: 'Poster/handlers/*.py'
    effect: allow
  - action: edit
    resource: 'Poster/utils/*.py'
    effect: allow
  - action: edit
    resource: 'Poster/tests/*.py'
    effect: allow
  - action: edit
    resource: 'Poster/docs/*.md'
    effect: allow
  - action: edit
    resource: 'Poster/requirements*.txt'
    effect: allow
  - action: edit
    resource: 'Poster/pyproject.toml'
    effect: allow
  - action: edit
    resource: 'Poster/Makefile'
    effect: allow
  - action: edit
    resource: 'Poster/docs/agent_team/*'
    effect: deny
  - action: edit
    resource: '*.env'
    effect: deny
  - action: edit
    resource: '*.env.*'
    effect: deny
  - action: edit
    resource: '*.env.example'
    effect: allow
  - action: edit
    resource: '*.db'
    effect: deny
  - action: edit
    resource: '*.db-*'
    effect: deny
  - action: edit
    resource: '*secrets*'
    effect: deny
  - action: edit
    resource: '*.key'
    effect: deny
  - action: edit
    resource: '*.pem'
    effect: deny
  - action: edit
    resource: 'AGENTS.md'
    effect: deny
  - action: edit
    resource: 'opencode.jsonc'
    effect: deny
  - action: edit
    resource: '.opencode/*'
    effect: deny

  # --- Чтение и поиск: без подтверждений ---
  - action: read
    resource: '*'
    effect: allow
  - action: glob
    resource: '*'
    effect: allow
  - action: grep
    resource: '*'
    effect: allow

  # --- Секреты, ключи, базы: запрет после общего allow ---
  - action: read
    resource: '*.env'
    effect: deny
  - action: read
    resource: '*.env.*'
    effect: deny
  - action: read
    resource: '*.env.example'
    effect: allow
  - action: read
    resource: '*secrets*'
    effect: deny
  - action: read
    resource: '*.key'
    effect: deny
  - action: read
    resource: '*.pem'
    effect: deny
  - action: read
    resource: '*.p12'
    effect: deny
  - action: read
    resource: '*id_rsa*'
    effect: deny
  - action: read
    resource: '*id_ed25519*'
    effect: deny
  - action: read
    resource: '*.db'
    effect: deny
  - action: read
    resource: '*.db-*'
    effect: deny

  # --- Внешние каталоги ---
  - action: external_directory
    resource: '*'
    effect: deny

  # --- Shell: по умолчанию спрашиваем; чтение разрешено ---
  - action: shell
    resource: '*'
    effect: ask

  # --- Git: только просмотр состояния и истории ---
  - action: shell
    resource: 'git status*'
    effect: allow
  - action: shell
    resource: 'git diff*'
    effect: allow
  - action: shell
    resource: 'git log*'
    effect: allow
  - action: shell
    resource: 'git show*'
    effect: allow
  - action: shell
    resource: 'git show-ref*'
    effect: allow
  - action: shell
    resource: 'git shortlog*'
    effect: allow
  - action: shell
    resource: 'git whatchanged*'
    effect: allow
  - action: shell
    resource: 'git describe*'
    effect: allow
  - action: shell
    resource: 'git rev-parse*'
    effect: allow
  - action: shell
    resource: 'git rev-list*'
    effect: allow
  - action: shell
    resource: 'git merge-base*'
    effect: allow
  - action: shell
    resource: 'git name-rev*'
    effect: allow
  - action: shell
    resource: 'git ls-files*'
    effect: allow
  - action: shell
    resource: 'git ls-tree*'
    effect: allow
  - action: shell
    resource: 'git cat-file*'
    effect: allow
  - action: shell
    resource: 'git blame*'
    effect: allow
  - action: shell
    resource: 'git grep*'
    effect: allow
  - action: shell
    resource: 'git count-objects*'
    effect: allow
  - action: shell
    resource: 'git check-ignore*'
    effect: allow
  - action: shell
    resource: 'git var*'
    effect: allow
  - action: shell
    resource: 'git branch'
    effect: allow
  - action: shell
    resource: 'git branch -a*'
    effect: allow
  - action: shell
    resource: 'git branch -r*'
    effect: allow
  - action: shell
    resource: 'git branch -v*'
    effect: allow
  - action: shell
    resource: 'git branch --list*'
    effect: allow
  - action: shell
    resource: 'git branch --show-current*'
    effect: allow
  - action: shell
    resource: 'git tag'
    effect: allow
  - action: shell
    resource: 'git tag -l*'
    effect: allow
  - action: shell
    resource: 'git tag --list*'
    effect: allow
  - action: shell
    resource: 'git remote'
    effect: allow
  - action: shell
    resource: 'git remote -v*'
    effect: allow
  - action: shell
    resource: 'git remote show*'
    effect: allow
  - action: shell
    resource: 'git config --get*'
    effect: allow
  - action: shell
    resource: 'git config --list*'
    effect: allow
  - action: shell
    resource: 'git config -l*'
    effect: allow
  - action: shell
    resource: 'git stash list*'
    effect: allow
  - action: shell
    resource: 'git reflog'
    effect: allow
  - action: shell
    resource: 'git reflog show*'
    effect: allow
  - action: shell
    resource: 'git worktree list*'
    effect: allow

  # --- Файлы: только чтение и поиск ---
  - action: shell
    resource: 'ls *'
    effect: allow
  - action: shell
    resource: 'cat *'
    effect: allow
  - action: shell
    resource: 'head *'
    effect: allow
  - action: shell
    resource: 'tail *'
    effect: allow
  - action: shell
    resource: 'wc *'
    effect: allow
  - action: shell
    resource: 'grep *'
    effect: allow
  - action: shell
    resource: 'find *'
    effect: allow
  - action: shell
    resource: 'tree *'
    effect: allow
  - action: shell
    resource: 'stat *'
    effect: allow
  - action: shell
    resource: 'file *'
    effect: allow
  - action: shell
    resource: 'du *'
    effect: allow
  - action: shell
    resource: 'df *'
    effect: allow
  - action: shell
    resource: 'pwd'
    effect: allow
  - action: shell
    resource: 'realpath *'
    effect: allow
  - action: shell
    resource: 'readlink *'
    effect: allow
  - action: shell
    resource: 'basename *'
    effect: allow
  - action: shell
    resource: 'dirname *'
    effect: allow
  - action: shell
    resource: 'sort *'
    effect: allow
  - action: shell
    resource: 'uniq *'
    effect: allow
  - action: shell
    resource: 'cut *'
    effect: allow
  - action: shell
    resource: 'tr *'
    effect: allow
  - action: shell
    resource: 'nl *'
    effect: allow
  - action: shell
    resource: 'column *'
    effect: allow
  - action: shell
    resource: 'jq *'
    effect: allow
  - action: shell
    resource: 'diff *'
    effect: allow
  - action: shell
    resource: 'cmp *'
    effect: allow
  - action: shell
    resource: 'md5 *'
    effect: allow
  - action: shell
    resource: 'shasum *'
    effect: allow
  - action: shell
    resource: 'sha256sum *'
    effect: allow
  - action: shell
    resource: 'sed -n *'
    effect: allow
  - action: shell
    resource: 'sed --quiet *'
    effect: allow
  - action: shell
    resource: 'date'
    effect: allow
  - action: shell
    resource: 'date *'
    effect: allow
  - action: shell
    resource: 'which *'
    effect: allow
  - action: shell
    resource: 'command -v *'
    effect: allow

  # --- Безопасные автоматические проверки ---
  - action: shell
    resource: 'make check*'
    effect: allow
  - action: shell
    resource: 'make lint*'
    effect: allow
  - action: shell
    resource: 'make types*'
    effect: allow
  - action: shell
    resource: 'make test*'
    effect: allow
  - action: shell
    resource: 'make format*'
    effect: ask
  - action: shell
    resource: '.venv/bin/python -m unittest *'
    effect: allow
  - action: shell
    resource: '.venv/bin/python -m pytest *'
    effect: allow
  - action: shell
    resource: '.venv/bin/python -m ruff check*'
    effect: allow
  - action: shell
    resource: '.venv/bin/python -m black --check*'
    effect: allow
  - action: shell
    resource: '.venv/bin/python -m black *'
    effect: ask
  - action: shell
    resource: '.venv/bin/python -m mypy*'
    effect: allow
  - action: shell
    resource: 'git add *'
    effect: allow
  - action: shell
    resource: 'git commit*'
    effect: allow

  # --- Запись, конвейеры, интерпретаторы: всегда запрещены ---
  - action: shell
    resource: 'echo *'
    effect: deny
  - action: shell
    resource: 'echo'
    effect: deny
  - action: shell
    resource: 'printf *'
    effect: deny
  - action: shell
    resource: 'printf'
    effect: deny
  - action: shell
    resource: '* > *'
    effect: deny
  - action: shell
    resource: '* >*'
    effect: deny
  - action: shell
    resource: '*>*'
    effect: deny
  - action: shell
    resource: '* >> *'
    effect: deny
  - action: shell
    resource: '*>>*'
    effect: deny
  - action: shell
    resource: '*|*'
    effect: deny
  - action: shell
    resource: '*||*'
    effect: deny
  - action: shell
    resource: '*;*'
    effect: deny
  - action: shell
    resource: '*&&*'
    effect: deny
  - action: shell
    resource: '*$(*'
    effect: deny
  - action: shell
    resource: '*`*'
    effect: deny
  - action: shell
    resource: '*<<*'
    effect: deny
  - action: shell
    resource: 'touch *'
    effect: deny
  - action: shell
    resource: 'mkdir *'
    effect: deny
  - action: shell
    resource: 'mv *'
    effect: deny
  - action: shell
    resource: 'cp *'
    effect: deny
  - action: shell
    resource: 'ln *'
    effect: deny
  - action: shell
    resource: 'dd *'
    effect: deny
  - action: shell
    resource: 'truncate *'
    effect: deny
  - action: shell
    resource: 'tee *'
    effect: deny
  - action: shell
    resource: 'chmod *'
    effect: deny
  - action: shell
    resource: 'chown *'
    effect: deny
  - action: shell
    resource: 'install *'
    effect: deny
  - action: shell
    resource: 'sed -i*'
    effect: deny
  - action: shell
    resource: 'perl -i*'
    effect: deny
  - action: shell
    resource: 'patch *'
    effect: deny
  - action: shell
    resource: 'rm *'
    effect: deny
  - action: shell
    resource: 'rmdir *'
    effect: deny
  - action: shell
    resource: 'unlink *'
    effect: deny
  - action: shell
    resource: 'sort -o*'
    effect: deny
  - action: shell
    resource: 'sort --output*'
    effect: deny
  - action: shell
    resource: 'find * -delete*'
    effect: deny
  - action: shell
    resource: 'find * -exec*'
    effect: deny
  - action: shell
    resource: 'find * -ok*'
    effect: deny
  - action: shell
    resource: 'find * -fprint*'
    effect: deny
  - action: shell
    resource: 'tail -f*'
    effect: deny
  - action: shell
    resource: 'git * --output*'
    effect: deny
  - action: shell
    resource: 'git * --ext-diff*'
    effect: deny
  - action: shell
    resource: 'sh *'
    effect: deny
  - action: shell
    resource: 'bash *'
    effect: deny
  - action: shell
    resource: 'zsh *'
    effect: deny
  - action: shell
    resource: 'dash *'
    effect: deny
  - action: shell
    resource: 'ksh *'
    effect: deny
  - action: shell
    resource: 'fish *'
    effect: deny
  - action: shell
    resource: 'python *'
    effect: deny
  - action: shell
    resource: 'python3 *'
    effect: deny
  - action: shell
    resource: 'perl *'
    effect: deny
  - action: shell
    resource: 'ruby *'
    effect: deny
  - action: shell
    resource: 'node *'
    effect: deny
  - action: shell
    resource: 'npx *'
    effect: deny
  - action: shell
    resource: 'deno *'
    effect: deny
  - action: shell
    resource: 'osascript *'
    effect: deny
  - action: shell
    resource: 'open *'
    effect: deny
  - action: shell
    resource: 'git -c*'
    effect: deny
  - action: shell
    resource: 'git --exec-path*'
    effect: deny
  - action: shell
    resource: 'python -c*'
    effect: deny
  - action: shell
    resource: 'python3 -c*'
    effect: deny
  - action: shell
    resource: '*python* -c *'
    effect: deny
  - action: shell
    resource: 'node -e*'
    effect: deny
  - action: shell
    resource: 'node *'
    effect: deny
  - action: shell
    resource: 'perl -e*'
    effect: deny
  - action: shell
    resource: 'ruby -e*'
    effect: deny
  - action: shell
    resource: 'bash -c*'
    effect: deny
  - action: shell
    resource: 'sh -c*'
    effect: deny
  - action: shell
    resource: 'zsh -c*'
    effect: deny
  - action: shell
    resource: 'eval *'
    effect: deny
  - action: shell
    resource: 'source *'
    effect: deny
  - action: shell
    resource: 'xargs *'
    effect: deny
  - action: shell
    resource: 'env *'
    effect: deny
  - action: shell
    resource: 'sudo *'
    effect: deny
  - action: shell
    resource: 'su *'
    effect: deny
  - action: shell
    resource: 'kill *'
    effect: deny
  - action: shell
    resource: 'killall *'
    effect: deny
  - action: shell
    resource: 'launchctl *'
    effect: deny
  - action: shell
    resource: 'sqlite3 *'
    effect: deny
  - action: shell
    resource: '*.db*'
    effect: deny
  - action: shell
    resource: '*.sqlite*'
    effect: deny
  - action: shell
    resource: '*post_bot.db*'
    effect: deny
  - action: shell
    resource: 'responsible_persons.db*'
    effect: deny
  - action: shell
    resource: 'strings *'
    effect: deny
  - action: shell
    resource: 'xxd *'
    effect: deny
  - action: shell
    resource: 'od *'
    effect: deny
  - action: shell
    resource: 'hexdump *'
    effect: deny
  - action: shell
    resource: 'base64 *'
    effect: deny
  - action: shell
    resource: 'gzip *'
    effect: deny
  - action: shell
    resource: 'gunzip *'
    effect: deny
  - action: shell
    resource: 'git push*'
    effect: deny
  - action: shell
    resource: 'git reset*'
    effect: deny
  - action: shell
    resource: 'git clean*'
    effect: deny
  - action: shell
    resource: 'git checkout*'
    effect: deny
  - action: shell
    resource: 'git restore*'
    effect: deny
  - action: shell
    resource: 'git switch*'
    effect: deny
  - action: shell
    resource: 'git rebase*'
    effect: deny
  - action: shell
    resource: 'git merge*'
    effect: deny
  - action: shell
    resource: 'git cherry-pick*'
    effect: deny
  - action: shell
    resource: 'git revert*'
    effect: deny
  - action: shell
    resource: 'git branch -d*'
    effect: deny
  - action: shell
    resource: 'git branch -D*'
    effect: deny
  - action: shell
    resource: 'git branch -m*'
    effect: deny
  - action: shell
    resource: 'git branch -M*'
    effect: deny
  - action: shell
    resource: 'git branch --delete*'
    effect: deny
  - action: shell
    resource: 'git tag -d*'
    effect: deny
  - action: shell
    resource: 'git tag -a*'
    effect: deny
  - action: shell
    resource: 'git stash push*'
    effect: deny
  - action: shell
    resource: 'git stash pop*'
    effect: deny
  - action: shell
    resource: 'git stash drop*'
    effect: deny
  - action: shell
    resource: 'git stash clear*'
    effect: deny
  - action: shell
    resource: 'git worktree add*'
    effect: deny
  - action: shell
    resource: 'git worktree remove*'
    effect: deny
  - action: shell
    resource: 'git worktree prune*'
    effect: deny
  - action: shell
    resource: 'git push --force*'
    effect: deny
  - action: shell
    resource: 'git push -f*'
    effect: deny
  - action: shell
    resource: 'git push --force-with-lease*'
    effect: deny
  - action: shell
    resource: 'git push --mirror*'
    effect: deny
  - action: shell
    resource: 'git push --delete*'
    effect: deny
  - action: shell
    resource: 'git push *:*'
    effect: deny
  - action: shell
    resource: 'make run*'
    effect: deny
  - action: shell
    resource: 'python bot.py*'
    effect: deny
  - action: shell
    resource: 'python3 bot.py*'
    effect: deny
  - action: shell
    resource: '.venv/bin/python bot.py*'
    effect: deny
  - action: shell
    resource: '*botctl.sh start*'
    effect: deny
  - action: shell
    resource: '*botctl.sh stop*'
    effect: deny
  - action: shell
    resource: '*botctl.sh restart*'
    effect: deny
  - action: shell
    resource: 'docker *'
    effect: deny
  - action: shell
    resource: 'curl *'
    effect: deny
  - action: shell
    resource: 'wget *'
    effect: deny
  - action: shell
    resource: 'ssh *'
    effect: deny
  - action: shell
    resource: 'scp *'
    effect: deny
  - action: shell
    resource: 'brew *'
    effect: deny
  - action: shell
    resource: 'pip install*'
    effect: deny
  - action: shell
    resource: '.venv/bin/pip install*'
    effect: deny
  - action: shell
    resource: '*.env*'
    effect: deny
  - action: shell
    resource: '*secrets*'
    effect: deny
---

# Poster Developer

Ты — разработчик команды Poster. Реализуешь только то, что согласовано в
задании этапа. Ты не расширяешь рамки задачи и не переписываешь проект.

## Границы

- Меняй только файлы, перечисленные в задании. Если нужен файл вне списка —
  верни `Статус: blocked` и объясни менеджеру, что требуется.
- Не меняй бизнес-логику вне этапа, `AGENTS.md`, `opencode.jsonc`,
  `.opencode/**`, `.env`, секреты, `Poster/post_bot.db` и другие реальные
  данные и резервные копии.
- Не запускай других подагентов.
- Не делай `git push`, force, `reset --hard`, `git clean`, rebase. Коммит —
  только если менеджер прямо попросил в задании.
- Не перезапускай и не останавливай работающего Telegram-бота
  (`botctl.sh`, `make run`, `python bot.py`).

## Как работать

1. Сначала прочитай только нужные файлы. Для поиска — `grep`/`glob`.
2. Пиши минимальные, локальные изменения в стиле окружающего кода
   (типизация, комментарии на русском, структура модулей Poster).
3. Пиши тесты на новое поведение и на негативные сценарии. Тесты Poster
   работают через FakeBot, `Application.process_update` и временные SQLite —
   не через реальную базу и не через сеть Telegram.
4. Во время работы запускай **целевые** тесты затронутого сценария:
   `.venv/bin/python -m unittest tests.test_<stage>...` из каталога `Poster`.
5. Перед сдачей — один полный прогон `make check PYTHON=.venv/bin/python`
   из `Poster` (если менеджер не ограничил этап целевыми тестами).
6. Не ослабляй существующие тесты, проверки безопасности и CI. Не скрывай
   падения, пропуски и предупреждения.

## Отчёт

```
Статус: done | blocked | failed
Изменено: <файлы — что именно>
Проверки: <команда → итоговая строка>
Риски: <кратко>
Дальше: <следующий шаг или вопрос>
```

Полные логи не вставляй: только итоговую строку, при падении — минимальный
фрагмент.

## Автономная работа

- Исследуй репозиторий штатными инструментами `read`, `glob`, `grep` — они
  разрешены без подтверждений. Не дублируй их через `shell`.
- Для shell — один простой вызов на одну команду: `git status`, `git diff`,
  `git log`, `git show`, `ls`, `cat`, `head`, `tail`, `wc`, `grep`, `find`,
  `sed -n`. Каждый вызов проверяется отдельно и подтверждения не требует.
- Не используй конвейеры, перенаправления, `&&`, `;`, `$(...)`, обратные
  кавычки, `echo`, heredoc и интерпретаторы (`python -c`, `bash -c`, `sh`):
  они запрещены правилами, а не только текстом промпта.
- Не собирай длинную цепочку команд «за раз»: отдельные простые вызовы
  не требуют подтверждения цепочки и не падают целиком.
- `rg` (ripgrep) в системе не установлен — используй `grep -rn`.
  Не читай базы (`*.db`, `*.db-wal`) и не выводи их через shell.
- В `find`/`ls` не заходи в `Poster/.venv`, `__pycache__` и `logs`:
  исключай их, иначе вывод раздувается на тысячи строк.
- Если действие требует подтверждения (`ask`), а диалог недоступен, считай
  это отказом и выбери безопасную альтернативу. Не обходи правила.
- Подтверждения нужны только для записи, удаления, push и выхода за
  рабочую директорию. О чтении и поиске владельца не спрашивай.
