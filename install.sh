#!/usr/bin/env bash
# Установка серверного экземпляра; локальный compose.yaml не используется.
set +x
set -Eeuo pipefail
umask 077

ROOT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
ENV_FILE="$ROOT_DIR/.env.server"
DOMAIN_FILE="$ROOT_DIR/deploy/server-domain.env"
SERVER_COMPOSE="$ROOT_DIR/compose.server.yaml"
PROJECT="zag-atlas-server"
TEMP_ENV=""

fail() { printf 'Ошибка: %s\n' "$*" >&2; return 1; }

cleanup() {
    if [[ -n "$TEMP_ENV" && "$TEMP_ENV" == "$ROOT_DIR"/.env.server.tmp.* ]]; then
        rm -f -- "$TEMP_ENV"
    fi
}

help_text() {
    cat <<'TEXT'
ZAG Atlas — установка отдельного серверного экземпляра на Linux.

Запуск: bash install.sh
Проверка требований без установки: bash install.sh --check
Справка: bash install.sh --help

Заранее нужны Docker с Compose, домен с DNS-записью на этот сервер,
свободные и доступные извне TCP-порты 80/443, ключ ProxyAPI.
Установщик спросит домен, ключ и первоначальный пароль администратора.
Ключ и пароль вводятся скрыто. Настройки сохраняются в .env.server.
При повторном запуске существующий .env.server используется без перезаписи.
Локальные .env, compose.yaml и данные проекта zag-atlas не изменяются.
TEXT
}

valid_domain() {
    local domain="$1" label
    local -a labels
    [[ ${#domain} -le 253 && "$domain" == *.* && "$domain" != *..* ]] || return 1
    [[ "$domain" != *[!a-z0-9.-]* ]] || return 1
    [[ ! "$domain" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]] || return 1
    IFS='.' read -r -a labels <<< "$domain"
    for label in "${labels[@]}"; do
        [[ ${#label} -ge 1 && ${#label} -le 63 ]] || return 1
        [[ "$label" =~ ^[a-z0-9]([a-z0-9-]*[a-z0-9])?$ ]] || return 1
    done
    [[ "$domain" != *. && "$domain" != .* ]]
}

valid_secret() {
    [[ -n "$1" && ${#1} -le 4096 && ! "$1" =~ [[:cntrl:]] ]]
}

env_raw_line() {
    # Compose format: raw передаёт символы значения без подстановок и кавычек.
    printf '%s=%s\n' "$1" "$2"
}

render_env() {
    local template="$1" domain="$2" key="$3" password="$4" line
    while IFS= read -r line || [[ -n "$line" ]]; do
        line="${line%$'\r'}"
        case "$line" in
            PROXYAPI_API_KEY=*|ADMIN_PASSWORD=*|SESSION_COOKIE_SECURE=*|ATLAS_DOMAIN=*) ;;
            *) printf '%s\n' "$line" ;;
        esac
    done < "$template"
    printf '\n# Отдельный серверный экземпляр\nATLAS_DOMAIN=%s\n' "$domain"
    env_raw_line PROXYAPI_API_KEY "$key"
    env_raw_line ADMIN_PASSWORD "$password"
    printf 'SESSION_COOKIE_SECURE=true\n'
}

compose() {
    docker compose --env-file "$DOMAIN_FILE" -f "$SERVER_COMPOSE" -p "$PROJECT" "$@"
}

preflight() {
    [[ "$(uname -s)" == Linux ]] || fail 'Запускайте install.sh в терминале Linux-сервера, а не на локальной Windows.'
    local command version major minor endpoint
    for command in docker curl getent ss mktemp; do
        command -v "$command" >/dev/null 2>&1 || fail "Не найдена команда $command. Подготовьте сервер по инструкции deploy/README.md."
    done
    version="$(docker compose version --short)" || fail 'Установите плагин Docker Compose.'
    version="${version#v}"
    IFS='.' read -r major minor _ <<< "$version"
    [[ "$major" =~ ^[0-9]+$ && "$minor" =~ ^[0-9]+$ ]] || fail 'Не удалось определить версию Compose.'
    (( major > 2 || (major == 2 && minor >= 30) )) || fail 'Нужен Docker Compose версии 2.30 или новее.'
    [[ "$(docker info --format '{{.OSType}}')" == linux ]] || fail 'Нет доступа к Docker Engine с Linux-контейнерами.'
    endpoint="${DOCKER_HOST:-}"
    if [[ -z "$endpoint" ]]; then
        endpoint="$(docker context inspect --format '{{.Endpoints.docker.Host}}')"
    fi
    [[ "$endpoint" == unix://* ]] || fail 'Нужен локальный Docker Engine этого Linux-сервера, без удалённого Docker-контекста.'
    [[ ! -L "$ENV_FILE" && ( ! -e "$ENV_FILE" || -f "$ENV_FILE" ) ]] || fail '.env.server должен быть обычным файлом.'
    for command in "$ROOT_DIR/Dockerfile" "$ROOT_DIR/.env.example" "$SERVER_COMPOSE" "$ROOT_DIR/deploy/Caddyfile"; do
        [[ -f "$command" ]] || fail "В комплекте отсутствует файл: $command"
    done
}

check_existing_data() {
    local volumes
    if [[ ! -f "$ENV_FILE" ]]; then
        volumes="$(docker volume ls -q --filter "label=com.docker.compose.project=$PROJECT")"
        [[ -z "$volumes" ]] || fail 'Найдены данные серверного экземпляра, но нет его .env.server. Восстановите исходный файл настроек; установщик данные не заменяет.'
    fi
}

prepare_domain_file() {
    local domain="$1" content
    [[ ! -L "$DOMAIN_FILE" ]] || fail 'Файл домена не должен быть символической ссылкой.'
    if [[ -e "$DOMAIN_FILE" ]]; then
        [[ -f "$DOMAIN_FILE" ]] || fail 'Файл домена должен быть обычным файлом.'
        content="$(cat "$DOMAIN_FILE")"
        content="${content%$'\r'}"
        [[ "$content" == "ATLAS_DOMAIN=$domain" ]] || fail 'Домен в deploy/server-domain.env отличается от .env.server. Согласуйте настройки перед повтором.'
    else
        (set -o noclobber; printf 'ATLAS_DOMAIN=%s\n' "$domain" > "$DOMAIN_FILE")
    fi
}

check_ports() {
    local own listeners
    own="$(docker ps -q --filter "label=com.docker.compose.project=$PROJECT" --filter 'label=com.docker.compose.service=caddy' --filter status=running)"
    if [[ -n "$own" ]]; then
        return 0
    fi
    listeners="$(ss -H -ltn '( sport = :80 or sport = :443 )')"
    [[ -z "$listeners" ]] || fail 'Порты 80 или 443 заняты. Согласуйте освобождение портов с администратором; установщик чужие сервисы не останавливает.'
}

existing_domain() {
    local line domain="" count=0
    while IFS= read -r line || [[ -n "$line" ]]; do
        line="${line%$'\r'}"
        if [[ "$line" == ATLAS_DOMAIN=* ]]; then
            domain="${line#ATLAS_DOMAIN=}"
            count=$((count + 1))
        fi
    done < "$ENV_FILE"
    if [[ $count -ne 1 ]] || ! valid_domain "$domain"; then
        fail 'Проверьте единственную строку ATLAS_DOMAIN=ваш.домен в .env.server.'
        return 1
    fi
    printf '%s' "$domain"
}

wait_for_https() {
    local domain="$1" attempts="${2:-12}" pause="${3:-5}" attempt body
    for ((attempt = 1; attempt <= attempts; attempt++)); do
        # Проверяем собственный Caddy по SNI, с настоящей проверкой сертификата.
        if body="$(curl --fail --silent --show-error --connect-timeout 5 --max-time 15 \
            --resolve "$domain:443:127.0.0.1" "https://$domain/health" 2>/dev/null)" && \
            [[ "$body" =~ \"status\"[[:space:]]*:[[:space:]]*\"ok\" ]]; then
            return 0
        fi
        if (( attempt < attempts )); then sleep "$pause"; fi
    done
    fail 'HTTPS не подтверждён. Проверьте DNS (включая AAAA), доступность портов 80/443 извне и логи Caddy. Контейнеры и данные сохранены.'
}

main() {
    local mode="${1:-}" domain key password confirmation
    [[ $# -le 1 ]] || { help_text; return 2; }
    case "$mode" in
        --help|-h) help_text; return 0 ;;
        ''|--check) ;;
        *) help_text; return 2 ;;
    esac
    trap cleanup EXIT
    trap 'printf "\nУстановка остановлена. Секреты в вывод не включены. Подробности: deploy/README.md.\n" >&2' ERR
    preflight
    check_existing_data
    check_ports
    if [[ "$mode" == --check ]]; then
        printf 'Docker, комплект и локальные порты проверены. DNS, сертификат и приложение этим режимом не проверялись.\n'
        return 0
    fi
    if [[ -f "$ENV_FILE" ]]; then
        domain="$(existing_domain)"
        printf 'Использую существующий .env.server без перезаписи.\n'
    else
        read -r -p 'Домен сайта (например atlas.company.ru, без https:// и пути): ' domain
        domain="${domain,,}"
        valid_domain "$domain" || fail 'Нужен корректный домен: латинские буквы, цифры и дефисы; кириллица — в Punycode.'
    fi
    getent ahosts "$domain" >/dev/null || fail 'Домен не разрешается в DNS. Сначала направьте его на сервер.'
    export ATLAS_DOMAIN="$domain"
    if [[ ! -f "$ENV_FILE" ]]; then
        read -r -s -p 'Ключ ProxyAPI (ввод скрыт): ' key
        printf '\n'
        valid_secret "$key" && [[ ! "$key" =~ [[:space:]] ]] || fail 'Ключ должен быть непустым, без пробелов и управляющих символов.'
        read -r -s -p 'Первоначальный пароль admin (8–128 символов, ввод скрыт): ' password
        printf '\n'
        valid_secret "$password" && (( ${#password} >= 8 && ${#password} <= 128 )) || fail 'Пароль должен содержать 8–128 символов без управляющих символов.'
        read -r -s -p 'Повторите пароль admin: ' confirmation
        printf '\n'
        [[ "$password" == "$confirmation" ]] || fail 'Пароли не совпадают. Файл настроек не создан.'
        TEMP_ENV="$(mktemp "$ROOT_DIR/.env.server.tmp.XXXXXX")"
        render_env "$ROOT_DIR/.env.example" "$domain" "$key" "$password" > "$TEMP_ENV"
        chmod 600 "$TEMP_ENV"
        # Создание через hard link атомарно отказывает при существующем файле.
        ln -- "$TEMP_ENV" "$ENV_FILE"
        cleanup
        TEMP_ENV=""
        unset key password confirmation
    fi
    prepare_domain_file "$domain"
    compose config --quiet >/dev/null 2>&1 || fail 'Конфигурация Compose не прошла проверку. Сверьте комплект и версию Compose по deploy/README.md.'
    printf 'Собираю и запускаю отдельный проект %s.\n' "$PROJECT"
    compose up -d --build --wait --wait-timeout 180
    printf 'Проверяю сертификат и ответ приложения через HTTPS.\n'
    wait_for_https "$domain"
    printf '\nСервер ответил по HTTPS: https://%s\n' "$domain"
    printf 'Откройте этот адрес с компьютера сотрудника и проверьте доступ извне.\n'
    printf 'Войдите как admin, смените первоначальный пароль, заполните профиль и загрузите документы.\n'
    printf 'Ответ с источниками и неизвестный вопрос проверьте через сайт: ключ модели этой проверкой не тестировался.\n'
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
    main "$@"
fi
