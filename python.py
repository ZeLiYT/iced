import os
import json
import uuid
import datetime
import logging
import time
from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, MessageHandler, ContextTypes, filters, ConversationHandler

# Для Google Drive API
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
from google.auth.transport.requests import Request
from google.auth.exceptions import RefreshError

# Настройка логирования
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Пути к файлам
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CREDENTIALS_FILE = os.path.join(BASE_DIR, 'credentials.json')
DATABASE_FILE = os.path.join(BASE_DIR, 'subscriptions.json')
TOKEN_FILE = os.path.join(BASE_DIR, 'token.json')

# Настройки Google Drive API
SCOPES = ['https://www.googleapis.com/auth/drive.file']

# Стадии для ConversationHandler
AWAITING_AUTH_CODE, AWAITING_CLIENT_NAME, AWAITING_CONFIGS = range(3)

# Админы бота (список Telegram ID пользователей, имеющих доступ)
ADMIN_USERS = [984155832]  # Замените на ваш Telegram ID

# Функции для работы с базой данных
def get_or_create_database():
    """Загрузка или создание базы данных подписок"""
    if os.path.exists(DATABASE_FILE):
        with open(DATABASE_FILE, 'r') as f:
            return json.load(f)
    else:
        # Создаем начальную структуру базы данных
        database = {
            "configs": [],
            "subscriptions": []
        }
        save_database(database)
        return database

def save_database(database):
    """Сохранение базы данных в файл"""
    with open(DATABASE_FILE, 'w') as f:
        json.dump(database, f, indent=2)

# Функции для Google Drive
async def get_drive_service(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Получение аутентифицированного сервиса Google Drive с обработкой обновления токена"""
    creds = None
    
    # Создаем папку для токена, если она не существует
    os.makedirs(os.path.dirname(TOKEN_FILE) or '.', exist_ok=True)
    
    try:
        if os.path.exists(TOKEN_FILE):
            with open(TOKEN_FILE, 'r') as token:
                try:
                    creds = Credentials.from_authorized_user_info(json.load(token), SCOPES)
                except json.JSONDecodeError as e:
                    logger.error(f"Invalid token file format: {e}")
                    # Файл поврежден, удаляем его
                    os.remove(TOKEN_FILE)
                    creds = None
        
        # Проверяем, нужно ли обновить токен или получить новый
        if creds and creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
                
                # Сохраняем обновленный токен
                with open(TOKEN_FILE, 'w') as token:
                    token.write(creds.to_json())
                    
                logger.info("Token refreshed successfully")
            except RefreshError as e:
                logger.error(f"Token refresh failed: {e}")
                creds = None  # Сбрасываем учетные данные для создания новых
                if os.path.exists(TOKEN_FILE):
                    os.remove(TOKEN_FILE)
        
        # Если нет действительных учетных данных, запускаем авторизацию
        if not creds or not creds.valid:
            # Создаем URL для авторизации
            flow = InstalledAppFlow.from_client_secrets_file(CREDENTIALS_FILE, SCOPES, redirect_uri='http://localhost:8080')
            auth_url, _ = flow.authorization_url(prompt='consent', access_type='offline')
            
            # Сохраняем flow в контексте для последующего использования
            context.user_data['oauth_flow'] = flow
            
            # Отправляем пользователю ссылку для авторизации
            await update.message.reply_text(
                f"Для авторизации в Google Drive перейдите по ссылке:\n{auth_url}\n\n"
                "После авторизации вы получите код. Скопируйте его и отправьте сюда."
            )
            return None
        
        # Если у нас уже есть валидные учетные данные, создаем сервис
        return build('drive', 'v3', credentials=creds, cache_discovery=False)
    
    except Exception as e:
        logger.error(f"Error in get_drive_service: {e}")
        # Сообщаем пользователю об ошибке
        if hasattr(update, 'message'):
            await update.message.reply_text(f"Произошла ошибка при подключении к Google Drive: {str(e)}")
        elif hasattr(update, 'callback_query'):
            await update.callback_query.message.reply_text(f"Произошла ошибка при подключении к Google Drive: {str(e)}")
        
        # В случае любой ошибки с токеном, начинаем процесс авторизации заново
        if os.path.exists(TOKEN_FILE):
            os.remove(TOKEN_FILE)
            
        # Создаем URL для авторизации
        flow = InstalledAppFlow.from_client_secrets_file(CREDENTIALS_FILE, SCOPES)
        auth_url, _ = flow.authorization_url(prompt='consent', access_type='offline')
        
        # Сохраняем flow в контексте для последующего использования
        context.user_data['oauth_flow'] = flow
        
        # Отправляем пользователю ссылку для авторизации
        if hasattr(update, 'message'):
            await update.message.reply_text(
                f"Требуется повторная авторизация. Перейдите по ссылке:\n{auth_url}\n\n"
                "После авторизации вы получите код. Скопируйте его и отправьте сюда."
            )
        elif hasattr(update, 'callback_query'):
            await update.callback_query.message.reply_text(
                f"Требуется повторная авторизация. Перейдите по ссылке:\n{auth_url}\n\n"
                "После авторизации вы получите код. Скопируйте его и отправьте сюда."
            )
        
        return None

async def handle_auth_code(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка кода авторизации Google с улучшенной обработкой ошибок"""
    auth_code = update.message.text.strip()
    flow = context.user_data.get('oauth_flow')
    
    if not flow:
        await update.message.reply_text("Сессия авторизации истекла. Начните с команды /start")
        return ConversationHandler.END
    
    try:
        # Обмениваем код на токен с явным запросом refresh_token
        flow.fetch_token(code=auth_code)
        creds = flow.credentials
        
        # Убедимся, что у нас есть refresh_token
        if not creds.refresh_token:
            logger.warning("No refresh_token received. Will request again with access_type=offline")
            # Если refresh_token отсутствует, начнем процесс заново с access_type=offline
            auth_url, _ = flow.authorization_url(prompt='consent', access_type='offline')
            
            await update.message.reply_text(
                f"Не удалось получить refresh_token. Пожалуйста, авторизуйтесь повторно:\n{auth_url}\n\n"
                "После авторизации вы получите код. Скопируйте его и отправьте сюда."
            )
            return AWAITING_AUTH_CODE
        
        # Сохраняем токен для будущих сессий с обработкой ошибок I/O
        try:
            # Создаем директорию, если она не существует
            os.makedirs(os.path.dirname(TOKEN_FILE) or '.', exist_ok=True)
            
            with open(TOKEN_FILE, 'w') as token:
                token.write(creds.to_json())
            
            # Проверка файловых прав
            os.chmod(TOKEN_FILE, 0o600)  # Только чтение и запись для владельца
            
            logger.info(f"Token saved successfully to {TOKEN_FILE}")
        except Exception as e:
            logger.error(f"Error saving token: {e}")
            await update.message.reply_text(f"Предупреждение: не удалось сохранить токен: {str(e)}")
        
        await update.message.reply_text("✅ Авторизация в Google Drive успешно завершена! 🎉")
        
        # Очищаем flow из контекста
        if 'oauth_flow' in context.user_data:
            del context.user_data['oauth_flow']
        
        # Показываем основное меню
        await show_main_menu(update, context)
        return ConversationHandler.END
        
    except Exception as e:
        logger.error(f"Ошибка авторизации: {e}")
        await update.message.reply_text(
            "Произошла ошибка при авторизации. Пожалуйста, убедитесь, что код верный, и попробуйте снова."
        )
        return AWAITING_AUTH_CODE

# Функции для управления подписками
def create_subscription_file(service, client_name, configs):
    """Создание файла подписки для клиента и возврат деталей файла"""
    # Создаем уникальное имя файла для клиента
    file_name = f"v2ray_sub_{client_name.replace(' ', '_').lower()}_{uuid.uuid4().hex[:8]}.txt"
    file_path = os.path.join(BASE_DIR, file_name)
    
    try:
        # Записываем конфигурации в файл
        with open(file_path, 'w') as f:
            f.write('\n'.join(configs))
        
        # Загружаем в Google Drive
        media = MediaFileUpload(file_path, mimetype='text/plain')
        file_metadata = {
            'name': file_name,
            'parents': ['root']
        }
        
        file = service.files().create(
            body=file_metadata,
            media_body=media,
            fields='id'
        ).execute()
        
        # Устанавливаем публичный доступ
        permission = {
            'type': 'anyone',
            'role': 'reader'
        }
        service.permissions().create(
            fileId=file['id'],
            body=permission
        ).execute()
        
        # Генерируем URL для скачивания
        download_url = f"https://drive.google.com/uc?id={file['id']}&export=download"
        
        return {
            "id": str(uuid.uuid4()),
            "name": client_name,
            "created_at": datetime.datetime.now().isoformat(),
            "file_id": file['id'],
            "download_url": download_url
        }
    finally:
        # Гарантированно закрываем и удаляем файл в блоке finally
        # Добавляем небольшую задержку и повторные попытки для Windows
        retry_count = 3
        for i in range(retry_count):
            try:
                if os.path.exists(file_path):
                    os.close(os.open(file_path, os.O_RDONLY))  # Попытка закрыть все дескрипторы файла
                    os.remove(file_path)
                break
            except Exception as e:
                if i < retry_count - 1:
                    time.sleep(0.5)  # Небольшая задержка перед следующей попыткой
                else:
                    logger.warning(f"Не удалось удалить временный файл {file_path}: {e}")

def update_subscription(service, subscription, configs):
    """Обновление существующей подписки новыми конфигурациями"""
    # Создаем временный файл
    temp_file = os.path.join(BASE_DIR, f"temp_{subscription['id']}.txt")
    
    try:
        # Записываем конфигурации в файл
        with open(temp_file, 'w') as f:
            f.write('\n'.join(configs))
        
        # Загружаем в Google Drive (обновляем)
        media = MediaFileUpload(temp_file, mimetype='text/plain')
        updated_file = service.files().update(
            fileId=subscription['file_id'],
            media_body=media
        ).execute()
        
        return True
    finally:
        # Гарантированно закрываем и удаляем файл в блоке finally
        retry_count = 3
        for i in range(retry_count):
            try:
                if os.path.exists(temp_file):
                    os.close(os.open(temp_file, os.O_RDONLY))  # Попытка закрыть все дескрипторы файла
                    os.remove(temp_file)
                break
            except Exception as e:
                if i < retry_count - 1:
                    time.sleep(0.5)  # Небольшая задержка перед следующей попыткой
                else:
                    logger.warning(f"Не удалось удалить временный файл {temp_file}: {e}")

async def update_all_subscriptions(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обновление всех подписок текущими конфигурациями"""
    # Проверка прав администратора
    if update.effective_user.id not in ADMIN_USERS:
        await update.callback_query.answer("У вас нет прав для выполнения этой операции")
        return
    
    await update.callback_query.answer()
    message = await update.callback_query.message.reply_text("⏳ Обновление всех подписок...")
    
    try:
        # Получаем сервис Google Drive
        service = await get_drive_service(update, context)
        if not service:
            return AWAITING_AUTH_CODE
        
        # Получаем данные
        database = get_or_create_database()
        configs = database["configs"]
        
        if not configs:
            await message.edit_text("❌ Нет доступных конфигураций. Пожалуйста, сначала добавьте конфигурации.")
            return
        
        if not database["subscriptions"]:
            await message.edit_text("❌ Нет подписок для обновления.")
            return
        
        # Обновляем каждую подписку
        update_count = 0
        for subscription in database["subscriptions"]:
            update_subscription(service, subscription, configs)
            update_count += 1
        
        await message.edit_text(f"✅ Успешно обновлено {update_count} подписок!")
        
    except Exception as e:
        logger.error(f"Ошибка при обновлении подписок: {e}")
        await message.edit_text(f"❌ Ошибка при обновлении подписок: {str(e)}")

# Команды бота
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик команды /start"""
    # Проверка прав администратора
    if update.effective_user.id not in ADMIN_USERS:
        await update.message.reply_text("⚠️ У вас нет прав доступа к этому боту.")
        return
    
    # Проверяем, есть ли уже токен для Google Drive
    if not os.path.exists(TOKEN_FILE):
        service = await get_drive_service(update, context)
        if not service:
            return AWAITING_AUTH_CODE
    
    await show_main_menu(update, context)
    return ConversationHandler.END

async def show_main_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Отображение главного меню бота"""
    database = get_or_create_database()
    config_count = len(database["configs"])
    subscription_count = len(database["subscriptions"])
    
    keyboard = [
        [InlineKeyboardButton("📝 Управление конфигурациями", callback_data="manage_configs")],
        [InlineKeyboardButton("🔄 Управление подписками", callback_data="manage_subscriptions")],
        [InlineKeyboardButton("➕ Создать подписку", callback_data="create_subscription")],
        [InlineKeyboardButton("🔄 Обновить все подписки", callback_data="update_all")]
    ]
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    message_text = (
        "🔰 *V2Ray Subscription Manager* 🔰\n\n"
        f"📊 *Статистика*:\n"
        f"• Конфигураций: {config_count}\n"
        f"• Активных подписок: {subscription_count}\n\n"
        "Выберите действие из меню ниже:"
    )
    
    # Определяем, нужно ли отправить новое сообщение или отредактировать существующее
    if update.callback_query:
        await update.callback_query.answer()
        await update.callback_query.message.edit_text(
            message_text,
            reply_markup=reply_markup,
            parse_mode="Markdown"
        )
    else:
        await update.message.reply_text(
            message_text,
            reply_markup=reply_markup,
            parse_mode="Markdown"
        )

async def manage_configs(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Управление конфигурациями V2Ray"""
    await update.callback_query.answer()
    
    database = get_or_create_database()
    configs = database["configs"]
    
    keyboard = [
        [InlineKeyboardButton("📝 Редактировать конфигурации", callback_data="edit_configs")],
        [InlineKeyboardButton("🏠 Вернуться в главное меню", callback_data="main_menu")]
    ]
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    message_text = (
        "📝 *Управление конфигурациями V2Ray*\n\n"
        f"Доступно {len(configs)} конфигураций.\n\n"
    )
    
    if configs:
        sample_configs = configs[:3]
        message_text += "Пример конфигураций:\n"
        for i, config in enumerate(sample_configs):
            # Показываем только первые 30 символов для компактности
            message_text += f"{i+1}. `{config[:30]}...`\n"
        
        if len(configs) > 3:
            message_text += f"...и еще {len(configs)-3} конфигураций\n"
    else:
        message_text += "❌ Конфигурации не найдены. Нажмите 'Редактировать конфигурации' для добавления."
    
    await update.callback_query.message.edit_text(
        message_text,
        reply_markup=reply_markup,
        parse_mode="Markdown"
    )

async def edit_configs(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Переход в режим редактирования конфигураций"""
    await update.callback_query.answer()
    
    message_text = (
        "📝 *Редактирование конфигураций V2Ray*\n\n"
        "Отправьте все ваши конфигурации в одном сообщении, по одной конфигурации на строку.\n"
        "Каждая строка должна быть в формате `vmess://...`, `trojan://...` и т.д.\n\n"
        "Существующие конфигурации будут заменены новыми."
    )
    
    keyboard = [[InlineKeyboardButton("🔙 Отмена", callback_data="manage_configs")]]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await update.callback_query.message.edit_text(
        message_text,
        reply_markup=reply_markup,
        parse_mode="Markdown"
    )
    
    return AWAITING_CONFIGS

async def save_configs(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Сохранение новых конфигураций"""
    configs_text = update.message.text
    configs = [line.strip() for line in configs_text.split('\n') if line.strip()]
    
    # Обновляем базу данных
    database = get_or_create_database()
    database["configs"] = configs
    save_database(database)
    
    await update.message.reply_text(f"✅ Успешно сохранено {len(configs)} конфигураций!")
    
    # Возвращаемся в главное меню
    await show_main_menu(update, context)
    return ConversationHandler.END

async def manage_subscriptions(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Управление подписками клиентов"""
    await update.callback_query.answer()
    
    database = get_or_create_database()
    subscriptions = database["subscriptions"]
    
    keyboard = [
        [InlineKeyboardButton("➕ Создать подписку", callback_data="create_subscription")],
        [InlineKeyboardButton("🔄 Обновить все подписки", callback_data="update_all")],
        [InlineKeyboardButton("🏠 Вернуться в главное меню", callback_data="main_menu")]
    ]
    
    if subscriptions:
        message_text = "🔄 *Список подписок*\n\n"
        
        for i, sub in enumerate(subscriptions):
            sub_id = sub["id"]
            message_text += f"{i+1}. *{sub['name']}*\n"
            message_text += f"   📅 Создано: {sub['created_at'].split('T')[0]}\n"
            message_text += f"   🔗 [Ссылка]({sub['download_url']})\n\n"
            
            # Добавляем кнопку удаления для каждой подписки
            keyboard.insert(i, [
                InlineKeyboardButton(f"❌ Удалить {sub['name']}", callback_data=f"delete_{sub_id}")
            ])
    else:
        message_text = "🔄 *Список подписок*\n\nПодписки не найдены. Создайте новую подписку!"
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await update.callback_query.message.edit_text(
        message_text,
        reply_markup=reply_markup,
        parse_mode="Markdown",
        disable_web_page_preview=True
    )

async def create_subscription_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показ формы для создания подписки"""
    await update.callback_query.answer()
    
    database = get_or_create_database()
    
    # Проверяем наличие конфигураций
    if not database["configs"]:
        keyboard = [[InlineKeyboardButton("📝 Добавить конфигурации", callback_data="edit_configs")]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await update.callback_query.message.edit_text(
            "❌ Нет доступных конфигураций. Пожалуйста, сначала добавьте конфигурации.",
            reply_markup=reply_markup
        )
        return
    
    message_text = (
        "➕ *Создание новой подписки*\n\n"
        "Отправьте имя клиента для создания подписки."
    )
    
    keyboard = [[InlineKeyboardButton("🔙 Отмена", callback_data="main_menu")]]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await update.callback_query.message.edit_text(
        message_text,
        reply_markup=reply_markup,
        parse_mode="Markdown"
    )
    
    return AWAITING_CLIENT_NAME

async def create_subscription_action(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка создания подписки"""
    client_name = update.message.text.strip()
    
    if not client_name:
        await update.message.reply_text("⚠️ Имя клиента не может быть пустым. Попробуйте еще раз.")
        return AWAITING_CLIENT_NAME
    
    await update.message.reply_text(f"⏳ Создание подписки для {client_name}...")
    
    try:
        # Получаем сервис Google Drive
        service = await get_drive_service(update, context)
        if not service:
            return AWAITING_AUTH_CODE
        
        # Получаем данные
        database = get_or_create_database()
        configs = database["configs"]
        
        # Создаем подписку
        subscription = create_subscription_file(service, client_name, configs)
        
        # Добавляем в базу данных
        database["subscriptions"].append(subscription)
        save_database(database)
        
        # Отправляем результат
        message_text = (
            f"✅ Подписка успешно создана для *{client_name}*!\n\n"
            f"🔗 Ссылка на подписку:\n`{subscription['download_url']}`"
        )
        
        await update.message.reply_text(
            message_text,
            parse_mode="Markdown"
        )
        
        # Возвращаемся в главное меню
        await show_main_menu(update, context)
        return ConversationHandler.END
        
    except Exception as e:
        logger.error(f"Ошибка создания подписки: {e}")
        await update.message.reply_text(f"❌ Ошибка создания подписки: {str(e)}")
        
        # Возвращаемся в главное меню
        await show_main_menu(update, context)
        return ConversationHandler.END

async def delete_subscription(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Удаление подписки"""
    await update.callback_query.answer()
    
    # Извлекаем ID подписки из callback_data
    callback_data = update.callback_query.data
    subscription_id = callback_data.replace("delete_", "")
    
    message = await update.callback_query.message.reply_text("⏳ Удаление подписки...")
    
    try:
        # Получаем данные
        database = get_or_create_database()
        
        # Находим подписку
        subscription = next((s for s in database["subscriptions"] if s["id"] == subscription_id), None)
        
        if not subscription:
            await message.edit_text("❌ Подписка не найдена")
            return
        
        # Получаем сервис Google Drive
        service = await get_drive_service(update, context)
        if not service:
            return AWAITING_AUTH_CODE
        
        # Удаляем из Google Drive
        service.files().delete(fileId=subscription["file_id"]).execute()
        
        # Удаляем из базы данных
        database["subscriptions"] = [s for s in database["subscriptions"] if s["id"] != subscription_id]
        save_database(database)
        
        await message.edit_text(f"✅ Подписка для {subscription['name']} успешно удалена!")
        
        # Обновляем список подписок
        await manage_subscriptions(update, context)
        
    except Exception as e:
        logger.error(f"Ошибка удаления подписки: {e}")
        await message.edit_text(f"❌ Ошибка удаления подписки: {str(e)}")

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Отмена текущей операции"""
    await update.message.reply_text("❌ Операция отменена.")
    await show_main_menu(update, context)
    return ConversationHandler.END

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик всех callback-запросов"""
    query = update.callback_query
    callback_data = query.data
    
    if callback_data == "main_menu":
        await show_main_menu(update, context)
    elif callback_data == "manage_configs":
        await manage_configs(update, context)
    elif callback_data == "manage_subscriptions":
        await manage_subscriptions(update, context)
    elif callback_data == "create_subscription":
        return await create_subscription_prompt(update, context)
    elif callback_data == "update_all":
        await update_all_subscriptions(update, context)
    elif callback_data == "edit_configs":
        return await edit_configs(update, context)
    elif callback_data.startswith("delete_"):
        await delete_subscription(update, context)
    else:
        await query.answer(f"Неизвестная команда: {callback_data}")

def main():
    """Основная функция для запуска бота"""
    # Получаем токен из файла или из переменной окружения
    # TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN") или из файла
    TELEGRAM_TOKEN = "7865242401:AAFi1WUf5QwzBVsj8Uc2eswtoDKSZb3qUiU"  # Замените на ваш токен
    
    # Создаем приложение
    application = Application.builder().token(TELEGRAM_TOKEN).build()
    
    # Обработчик диалога авторизации Google
    auth_conv_handler = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            AWAITING_AUTH_CODE: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_auth_code)]
        },
        fallbacks=[CommandHandler("cancel", cancel)]
    )
    
    # Обработчик диалога создания подписки
    subscription_conv_handler = ConversationHandler(
        entry_points=[CallbackQueryHandler(create_subscription_prompt, pattern="^create_subscription$")],
        states={
            AWAITING_CLIENT_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, create_subscription_action)]
        },
        fallbacks=[CommandHandler("cancel", cancel)]
    )
    
    # Обработчик диалога редактирования конфигураций
    configs_conv_handler = ConversationHandler(
        entry_points=[CallbackQueryHandler(edit_configs, pattern="^edit_configs$")],
        states={
            AWAITING_CONFIGS: [MessageHandler(filters.TEXT & ~filters.COMMAND, save_configs)]
        },
        fallbacks=[CommandHandler("cancel", cancel)]
    )
    
    # Добавляем обработчики
    application.add_handler(auth_conv_handler)
    application.add_handler(subscription_conv_handler)
    application.add_handler(configs_conv_handler)
    application.add_handler(CommandHandler("menu", start))
    application.add_handler(CallbackQueryHandler(handle_callback))
    
    # Запускаем бота
    application.run_polling()

if __name__ == "__main__":
    main()
