name: Publish AI News

# Когда запускать
on:
  schedule:
    # ВРЕМЯ ВАЖНО: GitHub Actions работает по UTC
    # МСК = UTC + 3 часа
    # 09:00 МСК = 06:00 UTC
    # 14:00 МСК = 11:00 UTC
    # 19:00 МСК = 16:00 UTC
    - cron: '0 6 * * *'   # 09:00 МСК
    - cron: '0 11 * * *'  # 14:00 МСК
    - cron: '0 16 * * *'  # 19:00 МСК

  # Можно также запускать вручную из вкладки Actions
  workflow_dispatch:

jobs:
  publish:
    runs-on: ubuntu-latest
    permissions:
      contents: write  # нужно чтобы коммитить файл с историей

    steps:
      - name: Получить код
        uses: actions/checkout@v4

      - name: Установить Python
        uses: actions/setup-python@v5
        with:
          python-version: '3.11'

      - name: Установить библиотеки
        run: |
          python -m pip install --upgrade pip
          pip install -r requirements.txt

      - name: Запустить бота
        env:
          TELEGRAM_BOT_TOKEN: ${{ secrets.TELEGRAM_BOT_TOKEN }}
          TELEGRAM_CHANNEL_ID: ${{ secrets.TELEGRAM_CHANNEL_ID }}
          ANTHROPIC_API_KEY: ${{ secrets.ANTHROPIC_API_KEY }}
        run: python bot.py

      - name: Сохранить историю в репозиторий
        run: |
          git config user.name "github-actions[bot]"
          git config user.email "github-actions[bot]@users.noreply.github.com"
          git add posted_news.json || true
          git diff --quiet && git diff --staged --quiet || git commit -m "Update posted news history [skip ci]"
          git push || true
