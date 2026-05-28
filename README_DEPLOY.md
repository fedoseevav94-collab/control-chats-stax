# Деплой Telegram-бота на Koyeb

Эта инструкция рассчитана на запуск бота как фонового worker-сервиса. Бот не открывает сайт и не слушает HTTP-порт, он постоянно держит polling Telegram.

## Что уже подготовлено

- Главный файл запуска: `main.py`
- Команда запуска: `python main.py`
- Procfile: `worker: python main.py`
- Зависимости: `requirements.txt`
- Секреты вынесены в переменные окружения
- `.env` добавлен в `.gitignore` и не должен попадать в GitHub

## 1. Загрузить проект в GitHub

1. Откройте папку проекта на компьютере.
2. Убедитесь, что файла `.env` не будет в репозитории. Он уже прописан в `.gitignore`.
3. Создайте новый репозиторий на GitHub.
4. Выполните команды в папке проекта:

```bash
git init
git add .
git commit -m "Prepare Telegram bot for Koyeb deploy"
git branch -M main
git remote add origin https://github.com/YOUR_USERNAME/YOUR_REPOSITORY.git
git push -u origin main
```

Замените `YOUR_USERNAME` и `YOUR_REPOSITORY` на свои значения.

## 2. Создать сервис в Koyeb

1. Откройте [Koyeb](https://www.koyeb.com/).
2. Войдите в аккаунт.
3. Нажмите `Create App` или `Deploy`.
4. Выберите источник `GitHub`.
5. Подключите ваш репозиторий с ботом.
6. В качестве типа сервиса выберите `Worker`, если такой выбор есть. Для Telegram polling web-порт не нужен.
7. Builder оставьте стандартный buildpack для Python.

## 3. Run command

Укажите:

```bash
python main.py
```

Если Koyeb использует `Procfile`, он возьмёт команду:

```text
worker: python main.py
```

## 4. Environment Variables

В настройках сервиса Koyeb откройте `Environment variables` и добавьте:

```env
BOT_TOKEN=ваш_токен_бота
TIMEZONE=Europe/Moscow
WORKDAY_START=09:30
WORKDAY_END=19:00
DAILY_REPORT_TIME=09:30
REMINDER_INTERVAL_MINUTES=15
DIRECT_MESSAGE_AFTER_MINUTES=60
LEADER_USERNAME=Fedos_AV
ESCALATE_AFTER_REMINDERS=3
MAX_GROUP_REMINDERS_IF_DM_UNREACHABLE=3
FINE_AMOUNT_RUBLES=500
BOT_UPDATE_DATE=28.05.2026
BOT_UPDATE_TIME=12:43
DATABASE_PATH=bot.sqlite3
SCHEDULER_STARTUP_GRACE_SECONDS=45
```

Обязательная переменная только одна:

```env
BOT_TOKEN=ваш_токен_бота
```

Остальные уже имеют значения по умолчанию, но лучше явно добавить их в Koyeb, чтобы настройки были видны.

## 5. Проверить Logs

После деплоя откройте страницу сервиса в Koyeb и перейдите во вкладку `Logs`.

Успешный запуск выглядит примерно так:

```text
Allowed updates: message, callback_query, message_reaction
Start polling
Run polling for bot @...
```

Если увидите:

```text
BOT_TOKEN не найден
```

значит переменная `BOT_TOKEN` не добавлена или названа с ошибкой.

Если увидите:

```text
Conflict: terminated by other getUpdates request
```

значит бот уже запущен где-то ещё: на ноутбуке, другом сервере или во втором сервисе Koyeb. Остановите лишнюю копию.

## 6. Перезапуск сервиса

В Koyeb откройте сервис бота и используйте кнопку `Redeploy` или `Restart`, если она доступна.

Также сервис перезапустится автоматически после нового push в GitHub, если включён autodeploy.

## 7. Важные замечания

- Не загружайте `.env` в GitHub. Токен должен храниться только в Koyeb Environment Variables.
- Для polling должен работать только один экземпляр бота с этим токеном.
- Для закрытия обращений по реакциям бот должен быть администратором в группе. Иначе Telegram не присылает `message_reaction`-обновления, даже если код их ожидает.
- SQLite на Koyeb может храниться в файловой системе конкретного инстанса. Если Koyeb пересоздаст инстанс, локальный `bot.sqlite3` может потеряться. Для полностью надёжного хранения позже лучше перейти на внешнюю базу, например PostgreSQL.
