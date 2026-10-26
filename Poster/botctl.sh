#!/bin/bash
# Управление основным экземпляром бота Poster (локальный запуск).
#
#   ./botctl.sh start    — запустить в фоне (nohup), PID в logs/post_bot.pid
#   ./botctl.sh stop     — остановить (SIGTERM, как Ctrl+C)
#   ./botctl.sh status   — показать состояние процесса
#   ./botctl.sh restart  — остановить и запустить заново
#
# Логи: logs/bot.log (ротация, настраивается log_config.py) и
# logs/stdout.log (поток stderr, включая предупреждения PTB).
# Повторный start при живом процессе безопасен — дубликата не создаёт.
# Конфигурация читается из .env, база — Poster/post_bot.db (миграции
# применяются при старте штатным init_db, повторный запуск идемпотентен).

set -euo pipefail
cd "$(dirname "$0")"

PYTHON=".venv/bin/python"
PID_FILE="logs/post_bot.pid"

is_running() {
    [ -f "$PID_FILE" ] && ps -p "$(cat "$PID_FILE")" >/dev/null 2>&1
}

case "${1:-status}" in
    start)
        if is_running; then
            echo "Уже запущен (PID $(cat "$PID_FILE"))."
            exit 0
        fi
        mkdir -p logs
        nohup "$PYTHON" bot.py >>logs/stdout.log 2>&1 &
        pid=$!
        echo "$pid" >"$PID_FILE"
        sleep 3
        if is_running; then
            echo "Запущен (PID $pid). Логи: logs/bot.log, logs/stdout.log"
        else
            rm -f "$PID_FILE"
            echo "Не удалось запустить — см. logs/stdout.log и logs/bot.log."
            exit 1
        fi
        ;;
    stop)
        if is_running; then
            pid=$(cat "$PID_FILE")
            kill "$pid"
            # ждём завершения до 10 секунд, затем сообщаем
            for _ in $(seq 1 20); do
                ps -p "$pid" >/dev/null 2>&1 || break
                sleep 0.5
            done
            if ps -p "$pid" >/dev/null 2>&1; then
                echo "Процесс $pid ещё завершается (SIGTERM отправлен)."
            else
                echo "Остановлен (PID $pid)."
            fi
            rm -f "$PID_FILE"
        else
            rm -f "$PID_FILE"
            echo "Не запущен."
        fi
        ;;
    status)
        if is_running; then
            echo "Запущен: PID $(cat "$PID_FILE") ($(ps -p "$(cat "$PID_FILE")" -o etime= | tr -d ' ') в работе)."
        else
            echo "Не запущен."
        fi
        ;;
    restart)
        "$0" stop
        "$0" start
        ;;
    *)
        echo "Использование: $0 {start|stop|status|restart}"
        exit 2
        ;;
esac
