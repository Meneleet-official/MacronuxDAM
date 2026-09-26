#!/usr/bin/env bash
# ==============================================================================
# PMX (`pmx` / `botctl.sh`) — Панель и CLI-утилита управления ботом на VPS
# ==============================================================================
set -euo pipefail

SERVICE_NAME="pmx-bot"

# Разрешаем симлинк (/usr/local/bin/pmx -> /path/to/botctl.sh)
SOURCE="${BASH_SOURCE[0]}"
while [[ -h "${SOURCE}" ]]; do
    DIR="$(cd -P "$(dirname "${SOURCE}")" && pwd)"
    SOURCE="$(readlink "${SOURCE}")"
    [[ "${SOURCE}" != /* ]] && SOURCE="${DIR}/${SOURCE}"
done
APP_DIR="$(cd -P "$(dirname "${SOURCE}")" && pwd)"

ENV_FILE="${APP_DIR}/.env"
DB_FILE="${APP_DIR}/data/modbot.db"
LOG_FILE="${APP_DIR}/data/modbot.log"
TELEMETRY_FILE="${APP_DIR}/data/telemetry.jsonl"
BACKUP_DIR="${APP_DIR}/backups"

SUDO=""
if [[ "${EUID:-$(id -u)}" -ne 0 ]] && command -v sudo >/dev/null 2>&1; then
    SUDO="sudo"
fi

# Цвета
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
PURPLE='\033[0;35m'
CYAN='\033[0;36m'
BOLD='\033[1m'
NC='\033[0m'

info()  { echo -e "${CYAN}[INFO]${NC} $*"; }
ok()    { echo -e "${GREEN}[OK]${NC} $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC} $*"; }
err()   { echo -e "${RED}[ERROR]${NC} $*" >&2; }

is_running() {
    systemctl is-active --quiet "${SERVICE_NAME}" 2>/dev/null
}

cmd_status() {
    echo -e "${PURPLE}${BOLD}=== 🛡️  Статус бота Macronux™ (PMX) ===${NC}"
    if is_running; then
        echo -e "Состояние:    ${GREEN}${BOLD}● РАБОТАЕТ (active/running)${NC}"
    else
        echo -e "Состояние:    ${RED}${BOLD}○ ОСТАНОВЛЕН (inactive/failed)${NC}"
    fi

    echo -e "Директория:   ${APP_DIR}"
    if [[ -f "${DB_FILE}" ]]; then
        DB_SIZE="$(du -h "${DB_FILE}" | cut -f1)"
        echo -e "База данных:  ${DB_FILE} (${DB_SIZE})"
    else
        echo -e "База данных:  ещё не создана"
    fi

    if [[ -f "${LOG_FILE}" ]]; then
        LOG_SIZE="$(du -h "${LOG_FILE}" | cut -f1)"
        echo -e "Файл логов:   ${LOG_FILE} (${LOG_SIZE})"
    fi

    echo ""
    ${SUDO} systemctl status "${SERVICE_NAME}" --no-pager -n 12 || true
}

cmd_start() {
    info "Запуск сервиса ${SERVICE_NAME}..."
    ${SUDO} systemctl start "${SERVICE_NAME}"
    sleep 1
    if is_running; then
        ok "Бот успешно запущен!"
    else
        err "Не удалось запустить бота. Проверьте логи: pmx logs"
        exit 1
    fi
}

cmd_stop() {
    info "Остановка сервиса ${SERVICE_NAME}..."
    ${SUDO} systemctl stop "${SERVICE_NAME}"
    ok "Бот остановлен."
}

cmd_restart() {
    info "Перезапуск сервиса ${SERVICE_NAME}..."
    ${SUDO} systemctl restart "${SERVICE_NAME}"
    sleep 1
    if is_running; then
        ok "Бот успешно перезапущен!"
    else
        err "Ошибка при перезапуске. Проверьте логи: pmx logs"
        exit 1
    fi
}

cmd_logs() {
    info "Просмотр живых логов (нажмите Ctrl+C для выхода)..."
    ${SUDO} journalctl -u "${SERVICE_NAME}" -f -n 100
}

cmd_errors() {
    echo -e "${YELLOW}${BOLD}=== Последние ошибки и предупреждения ===${NC}"
    if [[ -f "${LOG_FILE}" ]]; then
        grep -E "\[ERROR\]|\[WARNING\]|Traceback|Exception" "${LOG_FILE}" | tail -n 40 || echo "В файле логов ошибок не найдено."
    fi
    echo ""
    ${SUDO} journalctl -u "${SERVICE_NAME}" -p err..warning -n 30 --no-pager || true
}

cmd_backup() {
    mkdir -p "${BACKUP_DIR}"
    STAMP="$(date +%Y%m%d_%H%M%S)"
    TMP_BACKUP="$(mktemp -d)"
    ARCHIVE="${BACKUP_DIR}/pmx_backup_${STAMP}.tar.gz"

    info "Создание безопасной резервной копии..."
    if [[ -f "${DB_FILE}" ]]; then
        if command -v sqlite3 >/dev/null 2>&1; then
            sqlite3 "${DB_FILE}" ".backup '${TMP_BACKUP}/modbot.db'"
        else
            cp "${DB_FILE}" "${TMP_BACKUP}/modbot.db"
        fi
    fi
    [[ -f "${ENV_FILE}" ]] && cp "${ENV_FILE}" "${TMP_BACKUP}/.env"
    [[ -f "${TELEMETRY_FILE}" ]] && cp "${TELEMETRY_FILE}" "${TMP_BACKUP}/telemetry.jsonl"

    tar -czf "${ARCHIVE}" -C "${TMP_BACKUP}" .
    rm -rf "${TMP_BACKUP}"
    chmod 600 "${ARCHIVE}" || true
    ok "Резервная копия сохранена: ${BOLD}${ARCHIVE}${NC}"

    # Оставляем последние 15 бэкапов, старые удаляем
    ls -1t "${BACKUP_DIR}"/pmx_backup_*.tar.gz 2>/dev/null | tail -n +16 | xargs -r rm -f
}

cmd_restore() {
    TARGET_ARCHIVE="${1:-}"
    if [[ -z "${TARGET_ARCHIVE}" ]]; then
        echo "Доступные резервные копии в ${BACKUP_DIR}:"
        ls -1lh "${BACKUP_DIR}"/pmx_backup_*.tar.gz 2>/dev/null || { warn "Резервных копий нет."; exit 1; }
        echo ""
        read -r -p "Введите полный путь к архиву для восстановления: " TARGET_ARCHIVE
    fi
    if [[ ! -f "${TARGET_ARCHIVE}" ]]; then
        err "Файл не найден: ${TARGET_ARCHIVE}"
        exit 1
    fi

    warn "Бот будет временно остановлен для восстановления данных."
    cmd_stop

    TMP_RESTORE="$(mktemp -d)"
    tar -xzf "${TARGET_ARCHIVE}" -C "${TMP_RESTORE}"
    mkdir -p "${APP_DIR}/data"
    [[ -f "${TMP_RESTORE}/modbot.db" ]] && cp "${TMP_RESTORE}/modbot.db" "${DB_FILE}"
    [[ -f "${TMP_RESTORE}/telemetry.jsonl" ]] && cp "${TMP_RESTORE}/telemetry.jsonl" "${TELEMETRY_FILE}"
    [[ -f "${TMP_RESTORE}/.env" ]] && cp "${TMP_RESTORE}/.env" "${ENV_FILE}"
    rm -rf "${TMP_RESTORE}"

    ok "Данные восстановлены из ${TARGET_ARCHIVE}."
    cmd_start
}

cmd_update() {
    cd "${APP_DIR}"
    info "Создание резервной копии перед обновлением..."
    cmd_backup

    if [[ -d "${APP_DIR}/.git" ]]; then
        info "Получение свежего кода из Git-репозитория..."
        git pull --ff-only
    else
        warn "Папка ${APP_DIR} не является Git-репозиторием — пропускаем git pull."
    fi

    info "Обновление зависимостей в .venv..."
    "${APP_DIR}/.venv/bin/pip" install -r "${APP_DIR}/requirements.txt" -q
    chmod +x "${APP_DIR}/botctl.sh" "${APP_DIR}/install.sh"

    cmd_restart
    ok "Обновление завершено!"
}

cmd_config() {
    EDITOR_BIN="${EDITOR:-nano}"
    if ! command -v "${EDITOR_BIN}" >/dev/null 2>&1; then
        EDITOR_BIN="vi"
    fi
    "${EDITOR_BIN}" "${ENV_FILE}"
    chmod 600 "${ENV_FILE}" || true
    read -r -p "Перезапустить бота для применения новых настроек? [Y/n]: " RESTART_ANS
    if [[ "${RESTART_ANS:-Y}" =~ ^[YyДд]$ || -z "${RESTART_ANS:-}" ]]; then
        cmd_restart
    fi
}

cmd_stats() {
    echo -e "${PURPLE}${BOLD}=== 📊 Сводка базы данных Macronux™ (PMX) ===${NC}"
    if [[ ! -f "${DB_FILE}" ]]; then
        warn "База данных (${DB_FILE}) ещё не создана."
        return
    fi
    "${APP_DIR}/.venv/bin/python" - <<PYEOF
import sqlite3
conn = sqlite3.connect("${DB_FILE}")
cur = conn.cursor()
servers = cur.execute("SELECT COUNT(*) FROM servers").fetchone()[0]
violations = cur.execute("SELECT COUNT(*) FROM violations").fetchone()[0]
punishments = cur.execute("SELECT COUNT(*) FROM punishments WHERE action != 'dismiss'").fetchone()[0]
dismissed = cur.execute("SELECT COUNT(*) FROM punishments WHERE action = 'dismiss'").fetchone()[0]
banwords = cur.execute("SELECT COUNT(*) FROM banwords").fetchone()[0]
print(f"Серверов в БД:          {servers}")
print(f"Всего нарушений:        {violations}")
print(f"Вынесено наказаний:     {punishments}")
print(f"Снято (Не нарушение):   {dismissed}")
print(f"Кастомных банвордов:    {banwords}")
print("\nТоп-5 нарушаемых правил:")
for rule_id, cnt in cur.execute(
    "SELECT rule_id, COUNT(*) c FROM violations GROUP BY rule_id ORDER BY c DESC LIMIT 5"
):
    print(f"  • Правило {rule_id}: {cnt}")
conn.close()
PYEOF
}

cmd_uninstall() {
    read -r -p "Вы уверены, что хотите удалить автозапуск systemd и команду pmx? (База данных и .env сохранятся) [y/N]: " ANS
    if [[ ! "${ANS:-N}" =~ ^[YyДд]$ ]]; then
        info "Отменено."
        return
    fi
    ${SUDO} systemctl stop "${SERVICE_NAME}" 2>/dev/null || true
    ${SUDO} systemctl disable "${SERVICE_NAME}" 2>/dev/null || true
    ${SUDO} rm -f "/etc/systemd/system/${SERVICE_NAME}.service"
    ${SUDO} systemctl daemon-reload
    ${SUDO} rm -f "/usr/local/bin/pmx"
    ok "Сервис ${SERVICE_NAME} удалён. Файлы проекта и база данных в ${APP_DIR} сохранены."
}

show_help() {
    cat <<EOF
Использование: pmx [команда]

Команды:
  (без аргументов)  Открыть интерактивное меню управления
  status            Показать статус сервиса, аптайм и размеры файлов
  start             Запустить бота
  stop              Остановить бота
  restart           Перезапустить бота
  logs              Открыть живые логи бота (Ctrl+C для выхода)
  errors            Показать последние ошибки из логов
  stats             Показать статистику нарушений и наказаний из SQLite
  config            Открыть .env в редакторе и перезапустить бота
  update            Сделать бэкап, подтянуть обновления из Git и перезапустить
  backup            Сделать горячий бэкап БД, телеметрии и .env в backups/
  restore [файл]    Восстановить БД и настройки из архива бэкапа
  uninstall         Удалить systemd-сервис (с сохранением БД)
EOF
}

interactive_menu() {
    while true; do
        echo ""
        echo -e "${PURPLE}${BOLD}====================================================${NC}"
        echo -e "${PURPLE}${BOLD}   🛡️  Панель управления ботом Macronux™ (VPS)${NC}"
        echo -e "${PURPLE}${BOLD}====================================================${NC}"
        if is_running; then
            echo -e " Состояние: ${GREEN}${BOLD}● РАБОТАЕТ${NC}"
        else
            echo -e " Состояние: ${RED}${BOLD}○ ОСТАНОВЛЕН${NC}"
        fi
        echo "----------------------------------------------------"
        echo "  1) Статус бота и сервиса (status)"
        echo "  2) Живые логи в реальном времени (logs)"
        echo "  3) Последние ошибки (errors)"
        echo "  4) Статистика базы данных (stats)"
        echo "  5) Перезапустить бота (restart)"
        echo "  6) Запустить бота (start)"
        echo "  7) Остановить бота (stop)"
        echo "  8) Настроить .env / токены (config)"
        echo "  9) Обновить бота из Git + бэкап (update)"
        echo " 10) Создать резервную копию БД (backup)"
        echo " 11) Восстановить из резервной копии (restore)"
        echo "  0) Выход"
        echo "----------------------------------------------------"
        read -r -p "Выберите действие [0-11]: " CHOICE
        echo ""
        case "${CHOICE}" in
            1) cmd_status ;;
            2) cmd_logs ;;
            3) cmd_errors ;;
            4) cmd_stats ;;
            5) cmd_restart ;;
            6) cmd_start ;;
            7) cmd_stop ;;
            8) cmd_config ;;
            9) cmd_update ;;
            10) cmd_backup ;;
            11) cmd_restore ;;
            0|q|exit) exit 0 ;;
            *) warn "Неверный пункт меню." ;;
        esac
    done
}

ACTION="${1:-menu}"
shift || true

case "${ACTION}" in
    menu)      interactive_menu ;;
    status)    cmd_status ;;
    start)     cmd_start ;;
    stop)      cmd_stop ;;
    restart)   cmd_restart ;;
    logs|log)  cmd_logs ;;
    errors)    cmd_errors ;;
    stats)     cmd_stats ;;
    config)    cmd_config ;;
    update)    cmd_update ;;
    backup)    cmd_backup ;;
    restore)   cmd_restore "${1:-}" ;;
    uninstall) cmd_uninstall ;;
    help|-h|--help) show_help ;;
    *)
        err "Неизвестная команда: ${ACTION}"
        show_help
        exit 1
        ;;
esac
