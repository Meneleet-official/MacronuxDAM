#!/usr/bin/env bash
# ==============================================================================
# PMX (Macronux™ Moderation Bot) — Автоматический установщик на Linux VPS
# Поддерживаемые ОС: Ubuntu, Debian, CentOS/AlmaLinux/Rocky, Fedora, Arch Linux
# ==============================================================================
set -euo pipefail

SERVICE_NAME="pmx-bot"
CLI_LINK="/usr/local/bin/pmx"

# Цвета вывода
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

echo -e "${PURPLE}${BOLD}"
echo "=============================================================="
echo "   🛡️  Установщик бота модерации Macronux™ (PMX) на VPS"
echo "=============================================================="
echo -e "${NC}"

# Проверка прав sudo / root
SUDO=""
if [[ "${EUID:-$(id -u)}" -ne 0 ]]; then
    if command -v sudo >/dev/null 2>&1; then
        SUDO="sudo"
    else
        err "Запустите скрипт от имени root или установите sudo."
        exit 1
    fi
fi

# Определение директории проекта
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [[ -f "${SCRIPT_DIR}/main.py" && -f "${SCRIPT_DIR}/requirements.txt" ]]; then
    APP_DIR="${SCRIPT_DIR}"
else
    APP_DIR="/opt/pmx-bot"
    mkdir -p "${APP_DIR}"
fi

info "Рабочая директория бота: ${BOLD}${APP_DIR}${NC}"

# 1. Установка системных пакетов
info "Проверка и установка системных пакетов (Python 3, venv, git, sqlite3)..."
if command -v apt-get >/dev/null 2>&1; then
    ${SUDO} apt-get update -y -qq
    ${SUDO} apt-get install -y -qq python3 python3-venv python3-pip git sqlite3 curl tar
elif command -v dnf >/dev/null 2>&1; then
    ${SUDO} dnf install -y -q python3 python3-pip git sqlite curl tar
elif command -v yum >/dev/null 2>&1; then
    ${SUDO} yum install -y -q python3 python3-pip git sqlite curl tar
elif command -v pacman >/dev/null 2>&1; then
    ${SUDO} pacman -Sy --noconfirm python python-pip git sqlite curl tar
else
    warn "Неизвестный пакетный менеджер. Убедитесь, что python3, venv, git и sqlite3 установлены вручную."
fi
ok "Системные пакеты готовы."

# 2. Создание виртуального окружения и установка зависимостей
cd "${APP_DIR}"
mkdir -p "${APP_DIR}/data" "${APP_DIR}/backups"

if [[ ! -d "${APP_DIR}/.venv" ]]; then
    info "Создание виртуального окружения Python (.venv)..."
    python3 -m venv "${APP_DIR}/.venv"
fi

info "Обновление pip и установка зависимостей из requirements.txt..."
"${APP_DIR}/.venv/bin/python" -m pip install --upgrade pip wheel -q
"${APP_DIR}/.venv/bin/pip" install -r "${APP_DIR}/requirements.txt" -q
ok "Зависимости Python успешно установлены."

# 3. Первичная настройка .env (если не настроен)
ENV_FILE="${APP_DIR}/.env"
if [[ ! -f "${ENV_FILE}" ]]; then
    if [[ -f "${APP_DIR}/.env.example" ]]; then
        cp "${APP_DIR}/.env.example" "${ENV_FILE}"
    else
        cat > "${ENV_FILE}" <<'EOF'
BOT_TOKEN=
LLM_PROVIDER=groq
GROQ_API_KEY=
GROQ_MODEL=openai/gpt-oss-120b
GEMINI_API_KEY=
GEMINI_MODEL=gemini-2.0-flash-lite
DEEPSEEK_API_KEY=
DEEPSEEK_MODEL=deepseek-chat
OPENROUTER_API_KEY=
OPENROUTER_MODEL=meta-llama/llama-3.3-70b-instruct:free
STRIKE_DECAY_DAYS=60
DB_PATH=data/modbot.db
LOG_PATH=data/modbot.log
EOF
    fi
fi

chmod 600 "${ENV_FILE}" || true

CURRENT_TOKEN="$(grep -E '^BOT_TOKEN=' "${ENV_FILE}" | cut -d'=' -f2- | tr -d '[:space:]' || true)"
if [[ -z "${CURRENT_TOKEN}" || "${CURRENT_TOKEN}" == "YOUR_DISCORD_BOT_TOKEN_HERE" ]]; then
    echo ""
    echo -e "${YELLOW}${BOLD}⚙️  Первичная настройка конфигурации (.env)${NC}"
    read -r -p "Введите Discord BOT_TOKEN (или нажмите Enter, чтобы настроить позже): " INPUT_TOKEN
    if [[ -n "${INPUT_TOKEN}" ]]; then
        sed -i "s|^BOT_TOKEN=.*|BOT_TOKEN=${INPUT_TOKEN}|" "${ENV_FILE}"
        ok "BOT_TOKEN сохранён."
    fi

    echo ""
    echo "Выберите LLM-провайдера для ИИ-модерации и перевода:"
    echo "  1) groq       (Бесплатный тариф, быстрый — рекомендуется)"
    echo "  2) gemini     (Бесплатный тариф Google Gemini)"
    echo "  3) deepseek   (DeepSeek API)"
    echo "  4) openrouter (OpenRouter)"
    read -r -p "Ваш выбор [1-4, по умолчанию 1]: " PROV_CHOICE
    case "${PROV_CHOICE:-1}" in
        2) PROV="gemini"; KEY_VAR="GEMINI_API_KEY" ;;
        3) PROV="deepseek"; KEY_VAR="DEEPSEEK_API_KEY" ;;
        4) PROV="openrouter"; KEY_VAR="OPENROUTER_API_KEY" ;;
        *) PROV="groq"; KEY_VAR="GROQ_API_KEY" ;;
    esac
    sed -i "s|^LLM_PROVIDER=.*|LLM_PROVIDER=${PROV}|" "${ENV_FILE}"

    read -r -p "Введите API-ключ для ${PROV} (${KEY_VAR}, или Enter чтобы пропустить): " INPUT_API_KEY
    if [[ -n "${INPUT_API_KEY}" ]]; then
        if grep -qE "^${KEY_VAR}=" "${ENV_FILE}"; then
            sed -i "s|^${KEY_VAR}=.*|${KEY_VAR}=${INPUT_API_KEY}|" "${ENV_FILE}"
        else
            echo "${KEY_VAR}=${INPUT_API_KEY}" >> "${ENV_FILE}"
        fi
        ok "${KEY_VAR} сохранён."
    fi
fi

# 4. Настройка прав на скрипт управления botctl.sh и глобальной команды `pmx`
chmod +x "${APP_DIR}/botctl.sh" "${APP_DIR}/install.sh"
${SUDO} ln -sf "${APP_DIR}/botctl.sh" "${CLI_LINK}"
ok "Утилита управления установлена: теперь доступна команда ${BOLD}pmx${NC} из любой папки."

# 5. Создание и регистрация systemd-сервиса
RUN_USER="${SUDO_USER:-$(whoami)}"
SERVICE_FILE="/etc/systemd/system/${SERVICE_NAME}.service"

info "Создание systemd-сервиса ${SERVICE_FILE} (пользователь: ${RUN_USER})..."
${SUDO} tee "${SERVICE_FILE}" >/dev/null <<EOF
[Unit]
Description=Macronux (PMX) Discord Moderation Bot
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=${RUN_USER}
WorkingDirectory=${APP_DIR}
Environment="PYTHONUNBUFFERED=1"
Environment="PYTHONUTF8=1"
EnvironmentFile=${APP_DIR}/.env
ExecStart=${APP_DIR}/.venv/bin/python ${APP_DIR}/main.py
Restart=always
RestartSec=5
TimeoutStopSec=20
KillSignal=SIGTERM
StandardOutput=journal
StandardError=journal
SyslogIdentifier=${SERVICE_NAME}

[Install]
WantedBy=multi-user.target
EOF

${SUDO} systemctl daemon-reload
${SUDO} systemctl enable "${SERVICE_NAME}" >/dev/null

# Проверяем, задан ли BOT_TOKEN перед автозапуском
FINAL_TOKEN="$(grep -E '^BOT_TOKEN=' "${ENV_FILE}" | cut -d'=' -f2- | tr -d '[:space:]' || true)"
if [[ -n "${FINAL_TOKEN}" && "${FINAL_TOKEN}" != "YOUR_DISCORD_BOT_TOKEN_HERE" ]]; then
    info "Запуск сервиса ${SERVICE_NAME}..."
    ${SUDO} systemctl restart "${SERVICE_NAME}"
    sleep 2
    if ${SUDO} systemctl is-active --quiet "${SERVICE_NAME}"; then
        ok "Бот успешно запущен и работает в фоновом режиме (systemd)!"
    else
        warn "Сервис не смог стартовать. Проверьте логи командой: pmx logs"
    fi
else
    warn "BOT_TOKEN ещё не указан в ${ENV_FILE}."
    warn "Укажите токен командой: pmx config  и затем запустите бота: pmx start"
fi

echo ""
echo -e "${GREEN}${BOLD}==============================================================${NC}"
echo -e "${GREEN}${BOLD} ✅ Установка завершена! Управление ботом на VPS:${NC}"
echo -e "   • ${BOLD}pmx${NC}          — открыть интерактивное меню управления"
echo -e "   • ${BOLD}pmx status${NC}   — статус бота, RAM/CPU и статистика БД"
echo -e "   • ${BOLD}pmx logs${NC}     — живые логи в реальном времени"
echo -e "   • ${BOLD}pmx restart${NC}  — перезапустить бота"
echo -e "   • ${BOLD}pmx update${NC}   — обновить код из Git + перезапустить"
echo -e "   • ${BOLD}pmx backup${NC}   — сделать резервную копию БД и настроек"
echo -e "   • ${BOLD}pmx config${NC}   — редактировать .env (токены и ключи)"
echo -e "${GREEN}${BOLD}==============================================================${NC}"
