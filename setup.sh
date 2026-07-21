#!/usr/bin/env bash
# One-shot setup for Relay. Run from the repo root: ./setup.sh
set -e

echo "== Relay setup =="

# 1. Python 3.13+
PY=$(command -v python3 || true)
if [ -z "$PY" ]; then echo "✗ python3 не найден. Поставь Python 3.13+."; exit 1; fi
VER=$("$PY" -c 'import sys;print(f"{sys.version_info.major}.{sys.version_info.minor}")')
echo "• Python $VER"
"$PY" -c 'import sys;exit(0 if sys.version_info>=(3,13) else 1)' \
  || { echo "✗ Нужен Python 3.13+, у тебя $VER."; exit 1; }

# 2. Install the package (editable) -> `relay` command
echo "• Устанавливаю relay (pip install -e .)"
"$PY" -m pip install -e . >/dev/null

# 3. Frameworks (informational)
command -v opencode >/dev/null && echo "• opencode: $(command -v opencode)" \
  || echo "! opencode не найден — поставь: npm i -g opencode-ai (нужен для L0–L2)"
command -v claude >/dev/null && echo "• claude: $(command -v claude)" \
  || echo "! claude не найден — поставь Claude Code и залогинься (нужен для L3)"

# 4. OpenRouter key -> ~/.orchestrator/.env
ENV="$HOME/.orchestrator/.env"
mkdir -p "$HOME/.orchestrator"
if [ -f "$ENV" ] && grep -q OPENROUTER_API_KEY "$ENV"; then
  echo "• Ключ OpenRouter уже задан ($ENV)"
else
  printf 'OPENROUTER_API_KEY="sk-or-REPLACE-ME"\n' > "$ENV"
  chmod 600 "$ENV"
  echo "! Впиши свой ключ OpenRouter в $ENV (взять на https://openrouter.ai/keys)"
fi

echo ""
echo "Готово. Проверка: relay doctor"
echo "Запуск: cd <твой-проект> && relay"
