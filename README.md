# Hog Cards: Rebirth

Telegram-бот на Python и aiogram. Данные хранятся в SQLite; бот передаёт URL изображений Telegram, который загружает их самостоятельно. Только если Telegram отклоняет загрузку URL, бот скачивает изображение через `aiohttp` и повторяет отправку файлом.

## Запуск локально (Windows)

Требуется Python 3.12.

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
Copy-Item .env.example .env
```

Укажите токен бота в `.env`, затем запустите:

```powershell
python main.py
```

По умолчанию база создаётся в `data/card_bot.db`.

## Публикация и деплой

Добавляйте в GitHub исходный код и файлы зависимостей. Не публикуйте `.env`, SQLite-базу, папки `data`, `.venv` и резервные копии — они исключены через `.gitignore`.

Задайте `CARD_BOT_TOKEN` как секрет/переменную окружения на платформе деплоя. Для SQLite подключите постоянное хранилище: при Docker-деплое смонтируйте volume в `/app/data`, чтобы база сохранилась после перезапуска или обновления контейнера.

```sh
docker build -t hogcards-rebirth .
docker run -d --name hogcards-rebirth --restart unless-stopped \
  --env-file .env \
  -e CARD_BOT_DATA_PATH=/app/data/card_bot.db \
  -v hogcards-data:/app/data \
  hogcards-rebirth
```

`CARD_BOT_TOKEN` должен быть задан в `.env` локально или в секретах платформы. Не добавляйте реальные токены и пользовательские данные в GitHub.
