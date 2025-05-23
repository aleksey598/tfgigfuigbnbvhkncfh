#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import sys
import time
import logging
import re
import signal
import subprocess
import importlib.util
from typing import List, Dict, Tuple, Optional, Set, Union
import json
import sqlite3

# Функция для проверки и установки необходимых библиотек
def check_and_install_dependencies():
    required_packages = [
        "telethon", 
        "seleniumbase",
        "asyncio"
    ]
    
    for package in required_packages:
        if importlib.util.find_spec(package) is None:
            logging.info(f"Установка библиотеки {package}...")
            subprocess.check_call([sys.executable, "-m", "pip", "install", package])
            logging.info(f"Библиотека {package} успешно установлена.")

# Проверяем и устанавливаем библиотеки перед их импортом
check_and_install_dependencies()

# Теперь импортируем зависимости
import asyncio
from telethon import TelegramClient, events
from telethon.tl.types import Message, MessageMediaDocument, InputMessagesFilterDocument
from telethon.tl.functions.messages import ForwardMessagesRequest
from telethon.tl.functions.messages import GetHistoryRequest
from seleniumbase import Driver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import NoSuchElementException

# Настройка логирования
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler("telegram_report_downloader.log"),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# Конфигурация
CONFIG_FILE = "telegram_api_config.json"
DB_FILE = "telegram_report_downloader.db"
DOWNLOAD_DIR = os.path.abspath(os.path.join(os.getcwd(), "НАЗВАНИЕ ВАШЕЙ ПАПКИ"))
PROCESSED_LINKS_FILE = "processed_links.txt"
BOT_USERNAME = 'Test6271862571_bot'
MAX_FILES_PER_BATCH = 5  # Максимальное количество файлов для отправки за раз
MAX_RETRIES = 3
RETRY_DELAY = 0.5

# Класс для работы с базой данных
class Database:
    def __init__(self, db_file: str):
        self.db_file = db_file
        self.ensure_tables_exist()
    
    def ensure_tables_exist(self):
        """Создаём необходимые таблицы в базе данных, если они не существуют"""
        try:
            conn = sqlite3.connect(self.db_file)
            cursor = conn.cursor()
            
            # Таблица для хранения информации об обработанных файлах (для мониторинга)
            cursor.execute('''
            CREATE TABLE IF NOT EXISTS processed_files (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                file_id TEXT NOT NULL,
                original_message_id INTEGER NOT NULL,
                message_id INTEGER NOT NULL,
                user_id TEXT NOT NULL,
                processed_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                status TEXT DEFAULT 'pending'
            )
            ''')
            
            # Проверяем, существует ли столбец original_message_id
            cursor.execute("PRAGMA table_info(processed_files)")
            columns = [info[1] for info in cursor.fetchall()]
            if 'original_message_id' not in columns:
                cursor.execute('''
                ALTER TABLE processed_files ADD COLUMN original_message_id INTEGER NOT NULL DEFAULT 0
                ''')
            
            # Новая таблица для хранения пересланных файлов
            cursor.execute('''
            CREATE TABLE IF NOT EXISTS forwarded_files (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                file_id TEXT NOT NULL,
                user_id TEXT NOT NULL,
                original_message_id INTEGER NOT NULL,
                forwarded_message_id INTEGER NOT NULL,
                forwarded_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(file_id, user_id)
            )
            ''')
            
            # Таблица для хранения информации о сообщениях с ответами от бота
            cursor.execute('''
            CREATE TABLE IF NOT EXISTS bot_responses (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                message_id INTEGER NOT NULL,
                in_reply_to INTEGER,
                user_id TEXT NOT NULL,
                status TEXT NOT NULL,
                response_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            ''')
            
            # Таблица для хранения состояния процесса
            cursor.execute('''
            CREATE TABLE IF NOT EXISTS user_process_state (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT NOT NULL UNIQUE,
                state TEXT NOT NULL,
                data TEXT,
                last_updated TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            ''')
            
            conn.commit()
            conn.close()
        except sqlite3.Error as e:
            logger.error(f"Ошибка при создании таблиц в базе данных: {e}")
            if "database is locked" in str(e):
                time.sleep(1)
                self.ensure_tables_exist()
    
    def add_forwarded_file(self, file_id: str, original_message_id: int, forwarded_message_id: int, user_id: str):
        """Добавляет информацию о пересланном файле в таблицу forwarded_files"""
        try:
            conn = sqlite3.connect(self.db_file)
            cursor = conn.cursor()
            
            cursor.execute(
                "INSERT OR IGNORE INTO forwarded_files (file_id, original_message_id, forwarded_message_id, user_id) VALUES (?, ?, ?, ?)",
                (file_id, original_message_id, forwarded_message_id, user_id)
            )
            
            conn.commit()
            conn.close()
            return True
        except sqlite3.Error as e:
            logger.error(f"Ошибка при добавлении пересланного файла в базу данных: {e}")
            if "database is locked" in str(e):
                time.sleep(1)
                return self.add_forwarded_file(file_id, original_message_id, forwarded_message_id, user_id)
            return False
    
    def is_file_forwarded(self, file_id: str, user_id: str) -> bool:
        """Проверяет, был ли файл уже переслан для данного пользователя"""
        try:
            conn = sqlite3.connect(self.db_file)
            cursor = conn.cursor()
            
            cursor.execute(
                "SELECT id FROM forwarded_files WHERE file_id = ? AND user_id = ?",
                (file_id, user_id)
            )
            
            result = cursor.fetchone()
            conn.close()
            
            return result is not None
        except sqlite3.Error as e:
            logger.error(f"Ошибка при проверке пересланного файла в базе данных: {e}")
            if "database is locked" in str(e):
                time.sleep(1)
                return self.is_file_forwarded(file_id, user_id)
            return False
    
    def add_processed_file(self, file_id: str, original_message_id: int, message_id: int, user_id: str):
        """Добавляет информацию об обработанном файле в таблицу processed_files"""
        try:
            conn = sqlite3.connect(self.db_file)
            cursor = conn.cursor()
            
            cursor.execute(
                "INSERT INTO processed_files (file_id, original_message_id, message_id, user_id) VALUES (?, ?, ?, ?)",
                (file_id, original_message_id, message_id, user_id)
            )
            
            conn.commit()
            conn.close()
            return True
        except sqlite3.Error as e:
            logger.error(f"Ошибка при добавлении файла в базу данных: {e}")
            if "database is locked" in str(e):
                time.sleep(1)
                return self.add_processed_file(file_id, original_message_id, message_id, user_id)
            return False
    
    def is_file_processed(self, file_id: str, user_id: str) -> bool:
        """Проверяет, был ли файл уже обработан для данного пользователя (в processed_files)"""
        try:
            conn = sqlite3.connect(self.db_file)
            cursor = conn.cursor()
            
            cursor.execute(
                "SELECT id FROM processed_files WHERE file_id = ? AND user_id = ?",
                (file_id, user_id)
            )
            
            result = cursor.fetchone()
            conn.close()
            
            return result is not None
        except sqlite3.Error as e:
            logger.error(f"Ошибка при проверке файла в базе данных: {e}")
            if "database is locked" in str(e):
                time.sleep(1)
                return self.is_file_processed(file_id, user_id)
            return False
    
    def clear_pending_files(self, user_id: str):
        """Удаляет все файлы со статусом 'pending' для данного пользователя в processed_files"""
        try:
            conn = sqlite3.connect(self.db_file)
            cursor = conn.cursor()
            
            cursor.execute(
                "DELETE FROM processed_files WHERE user_id = ? AND status = 'pending'",
                (user_id,)
            )
            
            conn.commit()
            conn.close()
            logger.info(f"Очищены записи о файлах в статусе 'pending' для пользователя {user_id} в processed_files")
            return True
        except sqlite3.Error as e:
            logger.error(f"Ошибка при очистке файлов в статусе 'pending' для пользователя {user_id}: {e}")
            return False
    
    def add_bot_response(self, message_id: int, in_reply_to: int, user_id: str, status: str):
        """Добавляет информацию об ответе от бота"""
        try:
            conn = sqlite3.connect(self.db_file)
            cursor = conn.cursor()
            
            cursor.execute(
                "INSERT INTO bot_responses (message_id, in_reply_to, user_id, status) VALUES (?, ?, ?, ?)",
                (message_id, in_reply_to, user_id, status)
            )
            
            conn.commit()
            conn.close()
            return True
        except sqlite3.Error as e:
            logger.error(f"Ошибка при добавлении ответа бота в базу данных: {e}")
            if "database is locked" in str(e):
                time.sleep(1)
                return self.add_bot_response(message_id, in_reply_to, user_id, status)
            return False
    
    def get_pending_files(self, user_id: str) -> List[Tuple[int, str]]:
        """Возвращает список сообщений с файлами, ожидающими проверки в processed_files"""
        try:
            conn = sqlite3.connect(self.db_file)
            cursor = conn.cursor()
            
            cursor.execute(
                "SELECT message_id, status FROM processed_files WHERE user_id = ? AND status = 'pending'",
                (user_id,)
            )
            
            results = cursor.fetchall()
            conn.close()
            
            return results
        except sqlite3.Error as e:
            logger.error(f"Ошибка при получении ожидающих файлов: {e}")
            if "database is locked" in str(e):
                time.sleep(1)
                return self.get_pending_files(user_id)
            return []
    
    def update_file_status(self, message_id: int, user_id: str, status: str):
        """Обновляет статус обработки файла в processed_files"""
        try:
            conn = sqlite3.connect(self.db_file)
            cursor = conn.cursor()
            
            cursor.execute(
                "UPDATE processed_files SET status = ? WHERE message_id = ? AND user_id = ?",
                (status, message_id, user_id)
            )
            
            conn.commit()
            conn.close()
            return True
        except sqlite3.Error as e:
            logger.error(f"Ошибка при обновлении статуса файла: {e}")
            if "database is locked" in str(e):
                time.sleep(1)
                return self.update_file_status(message_id, user_id, status)
            return False
    
    def get_successful_responses(self, user_id: str) -> List[int]:
        """Получает ID сообщений с успешными ответами от бота для данного пользователя"""
        try:
            conn = sqlite3.connect(self.db_file)
            cursor = conn.cursor()
            
            cursor.execute(
                "SELECT message_id FROM bot_responses WHERE user_id = ? AND status = 'success'",
                (user_id,)
            )
            
            results = cursor.fetchall()
            conn.close()
            
            return [row[0] for row in results]
        except sqlite3.Error as e:
            logger.error(f"Ошибка при получении успешных ответов: {e}")
            if "database is locked" in str(e):
                time.sleep(1)
                return self.get_successful_responses(user_id)
            return []
    
    def save_user_process_state(self, user_id: str, state: str, data=None):
        """Сохраняет состояние процесса для пользователя"""
        try:
            conn = sqlite3.connect(self.db_file)
            cursor = conn.cursor()
            
            json_data = None
            if data is not None:
                json_data = json.dumps(data)
            
            cursor.execute(
                "INSERT OR REPLACE INTO user_process_state (user_id, state, data, last_updated) VALUES (?, ?, ?, CURRENT_TIMESTAMP)",
                (user_id, state, json_data)
            )
            
            conn.commit()
            conn.close()
            return True
        except sqlite3.Error as e:
            logger.error(f"Ошибка при сохранении состояния процесса для пользователя {user_id}: {e}")
            return False
    
    def get_user_process_state(self, user_id: str):
        """Получает состояние процесса для пользователя"""
        try:
            conn = sqlite3.connect(self.db_file)
            cursor = conn.cursor()
            
            cursor.execute(
                "SELECT state, data FROM user_process_state WHERE user_id = ?",
                (user_id,)
            )
            
            result = cursor.fetchone()
            conn.close()
            
            if result:
                state, json_data = result
                data = None
                if json_data:
                    try:
                        data = json.loads(json_data)
                    except json.JSONDecodeError:
                        logger.error(f"Ошибка декодирования JSON данных для пользователя {user_id}")
                
                return state, data
            else:
                return None, None
        except sqlite3.Error as e:
            logger.error(f"Ошибка при получении состояния процесса для пользователя {user_id}: {e}")
            return None, None
    
    def clear_user_process_state(self, user_id: str):
        """Удаляет состояние процесса для пользователя"""
        try:
            conn = sqlite3.connect(self.db_file)
            cursor = conn.cursor()
            
            cursor.execute(
                "DELETE FROM user_process_state WHERE user_id = ?",
                (user_id,)
            )
            
            conn.commit()
            conn.close()
            return True
        except sqlite3.Error as e:
            logger.error(f"Ошибка при удалении состояния процесса для пользователя {user_id}: {e}")
            return False

# Функции для работы с конфигурацией API
def load_api_config():
    """Загружает API конфигурацию из файла или запрашивает у пользователя"""
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, 'r') as f:
                config = json.load(f)
                return config.get('api_id'), config.get('api_hash')
        except Exception as e:
            logger.error(f"Ошибка при чтении файла конфигурации: {e}")
    
    # Запрашиваем API данные у пользователя
    print("Пожалуйста, введите ваши API данные.")
    api_id = input("Введите API ID: ")
    api_hash = input("Введите API Hash: ")
    
    # Сохраняем данные в файл
    try:
        with open(CONFIG_FILE, 'w') as f:
            json.dump({'api_id': api_id, 'api_hash': api_hash}, f)
    except Exception as e:
        logger.error(f"Ошибка при сохранении API данных: {e}")
    
    return api_id, api_hash

# Функция для обработки сигнала прерывания (Ctrl+C)
def signal_handler(sig, frame):
    logger.info("Получен сигнал прерывания. Завершение работы...")
    sys.exit(0)

signal.signal(signal.SIGINT, signal_handler)

# функция для пересылки файлов от пользователя к боту
async def forward_files_to_bot(client, user_id, bot_username, db):
    """Пересылает файлы от пользователя боту"""
    logger.info(f"Получение сообщений с файлами от пользователя {user_id}")
    
    try:
        # Получаем информацию о пользователе
        try:
            user_entity = await client.get_entity(user_id)
            logger.info(f"Успешно получена информация о пользователе: ID={user_entity.id}, Username={getattr(user_entity, 'username', 'None')}")
        except Exception as e:
            logger.error(f"Ошибка при получении информации о пользователе {user_id}: {e}")
            return 0
            
        bot_entity = await client.get_entity(bot_username)
        
        # Сохраняем состояние - начало пересылки файлов
        db.save_user_process_state(str(user_entity.id), "forwarding_files", {"bot_username": bot_username})
        
        # Получаем последние 10000 сообщений
        messages = await client.get_messages(user_entity, limit=10000)
        logger.info(f"Получено {len(messages)} последних сообщений из диалога с {user_id}")
        
        # Получаем собственный ID для сравнения
        me = await client.get_me()
        
        # Фильтруем только сообщения с файлами txt/docx/pdf/doc от другого пользователя
        valid_files = []
        for msg in messages:
            if msg.sender_id != me.id:
                logger.debug(f"Проверка сообщения ID={msg.id}: отправитель {msg.sender_id}")
                
                if msg.document:
                    logger.debug(f"Сообщение ID={msg.id} содержит документ: document_id={msg.document.id}")
                    
                    if not hasattr(msg, 'file') or not msg.file:
                        logger.warning(f"Сообщение ID={msg.id} от {user_id}: отсутствует объект file")
                        continue
                    if not hasattr(msg.file, 'name') or msg.file.name is None:
                        logger.warning(f"Сообщение ID={msg.id} от {user_id}: отсутствует имя файла")
                        continue
                    
                    file_name = msg.file.name.lower()
                    logger.debug(f"Найдено сообщение с файлом: {file_name}")
                    
                    if file_name and (file_name.endswith('.txt') or file_name.endswith('.docx') or file_name.endswith('.pdf') or file_name.endswith('.doc')):
                        if not db.is_file_forwarded(str(msg.document.id), str(user_entity.id)):
                            valid_files.append(msg)
                        else:
                            logger.debug(f"Файл {file_name} (file_id={msg.document.id}) уже переслан и будет пропущен")
                    else:
                        logger.debug(f"Файл {file_name} не соответствует поддерживаемым расширениям")
                else:
                    logger.debug(f"Сообщение ID={msg.id} не содержит документ")
            else:
                logger.debug(f"Сообщение ID={msg.id} от меня (sender_id={msg.sender_id}), пропускаем")
        
        total_files = len(valid_files)
        if total_files == 0:
            logger.warning(f"Не найдено новых файлов для обработки в диалоге с {user_id} (проверено {len(messages)} сообщений)")
            return 0
        
        logger.info(f"Найдено {total_files} новых файлов для обработки")
        
        # Пересылаем файлы боту партиями
        processed_count = 0
        batches = [valid_files[i:i+MAX_FILES_PER_BATCH] for i in range(0, len(valid_files), MAX_FILES_PER_BATCH)]
        
        for batch_idx, batch in enumerate(batches):
            logger.info(f"Пересылка партии {batch_idx+1}/{len(batches)} ({len(batch)} файлов)")
            
            try:
                forwarded_messages = await client.forward_messages(
                    entity=bot_entity,
                    messages=[msg.id for msg in batch],
                    from_peer=user_entity,
                    silent=False
                )
                
                # Обрабатываем успешно пересланные сообщения
                if isinstance(forwarded_messages, list):
                    if len(forwarded_messages) != len(batch):
                        logger.warning(f"Переслано {len(forwarded_messages)} сообщений из {len(batch)} в партии")
                    for i, msg in enumerate(batch):
                        if i < len(forwarded_messages):
                            forwarded_msg = forwarded_messages[i]
                            # Записываем в обе таблицы: forwarded_files и processed_files
                            db.add_forwarded_file(str(msg.document.id), msg.id, forwarded_msg.id, str(user_entity.id))
                            db.add_processed_file(str(msg.document.id), msg.id, forwarded_msg.id, str(user_entity.id))
                            processed_count += 1
                        else:
                            logger.error(f"Не удалось переслать файл {msg.file.name} (file_id={msg.document.id})")
                elif forwarded_messages:
                    # Записываем в обе таблицы для одиночного сообщения
                    db.add_forwarded_file(str(batch[0].document.id), batch[0].id, forwarded_messages.id, str(user_entity.id))
                    db.add_processed_file(str(batch[0].document.id), batch[0].id, forwarded_messages.id, str(user_entity.id))
                    processed_count += 1
            
            except Exception as e:
                logger.error(f"Ошибка при пересылке партии файлов: {e}")
            
            if batch_idx < len(batches) - 1:
                await asyncio.sleep(2)
            
            # Обновляем состояние после каждой партии
            db.save_user_process_state(str(user_entity.id), "forwarding_files_batch", 
                                      {"bot_username": bot_username, "batch_index": batch_idx+1, "total_batches": len(batches)})
        
        # Обновляем состояние - завершение пересылки файлов
        db.save_user_process_state(str(user_entity.id), "files_forwarded", 
                                 {"bot_username": bot_username, "processed_count": processed_count, "total_files": total_files})
        
        logger.info(f"Успешно переслано {processed_count} из {total_files} файлов")
        return processed_count
    
    except Exception as e:
        logger.error(f"Ошибка при пересылке файлов: {e}")
        return 0

# Функция для обновления строк прогресса в консоли
def update_progress_lines(waiting_files, total_files, processed_files, failed_files):
    """Обновляет строки прогресса в консоли без добавления новых строк"""
    # Первая строка: ожидающие проверки
    line1 = f"🕒 Ожидают проверки: {waiting_files}/{total_files}"
    
    # Вторая строка: успешно проверенные
    line2 = f"✅ Успешно проверено файлов: {processed_files - failed_files}/{total_files}"
    
    # Третья строка: неудачные проверки
    line3 = f"❌ Неудачных проверок: {failed_files}/{total_files}"
    
    # Очищаем три строки и выводим новое сообщение
    sys.stdout.write('\033[2K\r' + line1 + '\n\033[2K\r' + line2 + '\n\033[2K\r' + line3 + '\033[2A\r')
    sys.stdout.flush()

# Функция для мониторинга ответов бота
async def monitor_bot_responses(client, bot_username, user_id, db, resume_data=None):
    """Мониторит ответы от бота на пересланные файлы"""
    logger.info(f"Начинаем мониторинг ответов от бота {bot_username}")
    
    try:
        bot_entity = await client.get_entity(bot_username)
        user_entity = await client.get_entity(user_id)
        
        # Восстанавливаем данные из предыдущего состояния, если они есть
        total_files = 0
        processed_files = 0
        failed_files = 0
        successful_responses = []
        pending_message_ids = []
        
        if resume_data and 'pending_message_ids' in resume_data:
            # Восстанавливаем из предыдущего состояния
            pending_message_ids = resume_data.get('pending_message_ids', [])
            total_files = resume_data.get('total_files', 0)
            processed_files = resume_data.get('processed_files', 0)
            failed_files = resume_data.get('failed_files', 0)
            successful_responses = resume_data.get('successful_responses', [])
            
            logger.info(f"Восстановлен прогресс: {processed_files}/{total_files} обработано, {len(pending_message_ids)} ожидают")
        else:
            # Получаем список файлов, ожидающих проверки
            pending_files = db.get_pending_files(str(user_entity.id))
            pending_message_ids = [msg_id for msg_id, _ in pending_files]
            total_files = len(pending_message_ids)
            
            if not pending_message_ids:
                logger.info("Нет файлов, ожидающих проверки")
                return []
        
        # Сохраняем состояние - начало или продолжение мониторинга ответов бота
        db.save_user_process_state(str(user_entity.id), "monitoring_responses", {
            "bot_username": bot_username,
            "total_files": total_files,
            "processed_files": processed_files,
            "failed_files": failed_files,
            "pending_message_ids": pending_message_ids,
            "successful_responses": successful_responses
        })
        
        # Выводим начальный прогресс (три строки)
        update_progress_lines(len(pending_message_ids), total_files, processed_files, failed_files)
        
        # Продолжаем мониторинг, пока все файлы не будут обработаны
        start_time = time.time()
        max_wait_time = 7200  # Максимальное время ожидания - 2 часа
        
        while pending_message_ids and time.time() - start_time < max_wait_time:
            # Получаем свежие сообщения от бота
            messages = await client.get_messages(bot_entity, limit=10000)
            
            for message in messages:
                # Проверяем, является ли сообщение ответом на один из наших файлов
                if message.reply_to and message.reply_to.reply_to_msg_id in pending_message_ids:
                    original_msg_id = message.reply_to.reply_to_msg_id
                    
                    # Проверяем текст сообщения
                    if message.text:
                        if "Ваш файл успешно проверен" in message.text:
                            # Успешная проверка
                            db.update_file_status(original_msg_id, str(user_entity.id), "success")
                            db.add_bot_response(message.id, original_msg_id, str(user_entity.id), "success")
                            pending_message_ids.remove(original_msg_id)
                            processed_files += 1
                            successful_responses.append(message.id)
                        
                        elif "Произошла ошибка при проверке документа" in message.text:
                            # Ошибка проверки
                            db.update_file_status(original_msg_id, str(user_entity.id), "error")
                            db.add_bot_response(message.id, original_msg_id, str(user_entity.id), "error")
                            pending_message_ids.remove(original_msg_id)
                            processed_files += 1
                            failed_files += 1  # Увеличиваем счетчик неудачных проверок
                            
                            original_message = await client.get_messages(bot_entity, ids=original_msg_id)
                            if original_message and original_message.file:
                                logger.error(f"Ошибка при проверке файла: {original_message.file.name}")
            
            # Обновляем информацию о прогрессе
            update_progress_lines(len(pending_message_ids), total_files, processed_files, failed_files)
            
            # Сохраняем текущий прогресс
            db.save_user_process_state(str(user_entity.id), "monitoring_responses_progress", {
                "bot_username": bot_username,
                "total_files": total_files,
                "processed_files": processed_files,
                "failed_files": failed_files,
                "pending_message_ids": pending_message_ids,
                "successful_responses": successful_responses
            })
            
            # Если остались необработанные файлы, продолжаем мониторинг
            if pending_message_ids:
                await asyncio.sleep(1)  # Проверяем каждую секунду
        
        sys.stdout.write('\n\n\n') 
        
        if pending_message_ids:
            logger.warning(f"Превышено время ожидания. Не обработано {len(pending_message_ids)} файлов")
        else:
            logger.info(f"Все {total_files} файлов обработаны. Успешных: {total_files - failed_files}, неудачных: {failed_files}")
        
        # Сохраняем состояние - завершение мониторинга
        db.save_user_process_state(str(user_entity.id), "responses_monitored", {
            "bot_username": bot_username,
            "total_files": total_files,
            "processed_files": processed_files,
            "failed_files": failed_files,
            "successful_responses": successful_responses
        })
        
        return successful_responses
    
    except Exception as e:
        logger.error(f"Ошибка при мониторинге ответов бота: {e}")
        return []

# Функция для отправки скачанных отчетов пользователю
async def send_reports_to_user(client, user_id, report_paths):
    """Отправляет скачанные отчеты пользователю"""
    logger.info(f"Отправка скачанных отчетов пользователю {user_id}")
    
    if not report_paths:
        logger.warning("Нет отчетов для отправки")
        return 0
    
    try:
        user_entity = await client.get_entity(user_id)
        existing_files = [path for path in report_paths if os.path.exists(path)]
        
        if not existing_files:
            logger.warning("Ни один из указанных файлов отчетов не найден")
            return 0
        
        # Выводим информацию о количестве файлов для отправки
        file_count = len(existing_files)
        print(f"Подготовка к отправке {file_count} файлов пользователю...")
        
        # Выводим список файлов и их размеры для наглядности
        total_size = 0
        for path in existing_files:
            size = os.path.getsize(path)
            total_size += size
            file_name = os.path.basename(path)
            print(f"  - {file_name} ({size / 1024 / 1024:.2f} МБ)")
        
        print(f"Общий размер файлов: {total_size / 1024 / 1024:.2f} МБ")
        print("Отправка всех файлов ")
        
        # Отправляем все файлы
        await client.send_file(
            entity=user_entity,
            file=existing_files
        )
        
        logger.info(f"Успешно отправлены все {file_count} отчетов")
        print(f"✅ Все {file_count} файлов успешно отправлены!")
        return file_count
    
    except Exception as e:
        logger.error(f"Ошибка при отправке отчетов: {e}")
        print(f"❌ Ошибка при отправке отчетов: {e}")
        return 0

def sanitize_filename(filename):
    """Очистка имени файла от недопустимых символов с поддержкой русского языка"""
    if not filename:
        return f"report_{int(time.time())}"
    
    # Список недопустимых символов для имени файла
    invalid_chars = ['/', '\\', ':', '*', '?', '"', '<', '>', '|']
    
    # Заменяем только недопустимые символы на подчеркивание
    sanitized = filename
    for char in invalid_chars:
        sanitized = sanitized.replace(char, '_')
    
    # Удаляем начальные и конечные пробелы
    sanitized = sanitized.strip()
    
    # Ограничиваем длину имени файла
    if len(sanitized) > 200:
        sanitized = sanitized[:197] + '...'
    
    # Если после очистки имя пустое или состоит только из подчеркиваний
    if not sanitized or sanitized.strip('_') == '':
        sanitized = f"report_{int(time.time())}"
        
    return sanitized

def setup_driver():
    """Настройка и инициализация веб-драйвера"""
    logger.info("Инициализация веб-драйвера...")
    
    # Определение директории для загрузок
    os.makedirs(DOWNLOAD_DIR, exist_ok=True)
    
    # Создаем драйвер
    driver = Driver(uc=True, headless=True)
    driver.set_window_size(650, 1200)
    
    # Настройка директории для загрузок
    try:
        driver.execute_cdp_cmd('Page.setDownloadBehavior', {
            'behavior': 'allow',
            'downloadPath': DOWNLOAD_DIR
        })
    except Exception:
        pass
    
    # Проверка работоспособности драйвера
    driver.get("about:blank")
    
    return driver

def get_processed_links():
    """Получение списка уже обработанных ссылок из файла"""
    processed_links = []
    
    if os.path.exists(PROCESSED_LINKS_FILE):
        try:
            with open(PROCESSED_LINKS_FILE, 'r', encoding='utf-8') as file:
                content = file.read()
                # Извлечение ссылок
                matches = re.findall(r'\d+\.\s+(https?://\S+)', content)
                if matches:
                    processed_links = matches
        except Exception as e:
            logger.error(f"Ошибка при чтении файла: {e}")
    
    return processed_links

def save_processed_link(link):
    """Сохранение обработанной ссылки в файл"""
    processed_links = get_processed_links()
    
    # Добавление новой ссылки, если её ещё нет в списке
    if link not in processed_links:
        processed_links.append(link)
    
    # Запись всех ссылок в файл
    try:
        with open(PROCESSED_LINKS_FILE, 'w', encoding='utf-8') as file:
            for i, processed_link in enumerate(processed_links, 1):
                file.write(f"{i}. {processed_link}\n")
    except Exception as e:
        logger.error(f"Ошибка при сохранении ссылки в файл: {e}")

def get_title_from_page(driver):
    """Получение названия документа со страницы с улучшенной поддержкой русского языка"""
    try:
        # Ожидание загрузки страницы и появления заголовка
        WebDriverWait(driver, 5).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, "div.title h2"))
        )
        
        try:
            title = driver.execute_script("return document.querySelector('div.title h2').textContent")
            if not title:
                raise Exception("Пустой заголовок")
        except Exception:
            try:
                title_element = driver.find_element(By.CSS_SELECTOR, "div.title h2")
                title = title_element.text
            except NoSuchElementException:
                try:
                    title_element = driver.find_element(By.XPATH, "//div[contains(@class, 'title')]/h2")
                    title = title_element.text
                except Exception:
                    return f"report_{int(time.time())}"
        
        # Удаление расширения  из названия
        title = title.replace('.docx', '').replace('.txt', '').replace('.pdf', '').replace('.doc', '').strip()
        
        # Логирование оригинального названия
        logger.info(f"Оригинальное название документа: '{title}'")
        
        if not title:
            return f"report_{int(time.time())}"
            
        return title
    except Exception as e:
        logger.error(f"Ошибка при получении названия документа: {e}")
        return f"report_{int(time.time())}"

def wait_for_download_complete(timeout=120):
    """Ожидание завершения загрузки файла с увеличенным таймаутом для больших файлов"""
    start_time = time.time()
    files_before = set(os.listdir(DOWNLOAD_DIR))
    
    while time.time() - start_time < timeout:
        current_files = set(os.listdir(DOWNLOAD_DIR))
        new_files = current_files - files_before
        
        # Проверяем, есть ли среди новых файлов временные файлы загрузки
        downloading_files = [f for f in new_files if f.endswith('.crdownload') or f.endswith('.part') or f.endswith('.tmp')]
        
        if new_files and not downloading_files:
            return list(new_files)
        
        time.sleep(1) 
    
    # Если время вышло, проверим, появились ли какие-то новые файлы
    current_files = set(os.listdir(DOWNLOAD_DIR))
    new_files = current_files - files_before
    
    if new_files:
        # Проверяем, есть ли среди новых файлов временные файлы загрузки
        downloading_files = [f for f in new_files if f.endswith('.crdownload') or f.endswith('.part') or f.endswith('.tmp')]
        if not downloading_files:
            return list(new_files)
    
    return []

def download_report(driver, url, retry_count=0):
    """Скачивание отчета по ссылке через нажатие кнопки 'Скачать'"""
    if retry_count >= MAX_RETRIES:
        logger.error(f"Превышено максимальное количество попыток для ссылки {url}")
        return False
    
    new_files = []
    
    try:
        # Открытие ссылки
        logger.info("Открытие ссылки")
        driver.get(url)
        logger.info("Ссылка успешно открыта")
        
        # Ожидание загрузки страницы
        WebDriverWait(driver, 10).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, "div.ap-report-nav"))
        )
        
        # Открытие выпадающего меню
        logger.info("Открытие выпадающего меню")
        menu_button = WebDriverWait(driver, 5).until(
            EC.element_to_be_clickable((By.CSS_SELECTOR, "div.ap-report-nav i.ap-report-btn"))
        )
        menu_button.click()
        logger.info("Выпадающее меню открыто успешно")
        
        # Нажатие на кнопку "Экспорт" сразу после появления
        logger.info("Нажатие на кнопку 'Экспорт'")
        export_button = WebDriverWait(driver, 5).until(
            EC.element_to_be_clickable((By.CSS_SELECTOR, "a.report-export"))
        )
        export_url = export_button.get_attribute('href')
        
        # Открываем URL экспорта в том же окне
        logger.info("Переход на страницу экспорта")
        driver.get(export_url)
        logger.info("Переход на страницу экспорта выполнен успешно")
        
        # Получаем название документа
        document_title = get_title_from_page(driver)
        logger.info(f"Название документа: {document_title}")
        
        # Быстрая проверка наличия кнопки "Скачать" 
        download_button = None
        try:
            # Используем короткий таймаут для быстрой проверки
            download_button = WebDriverWait(driver, 2).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, "button.export-download"))
            )
        except Exception:
            # Если кнопка "Скачать" не найдена, сразу нажимаем на кнопку "Экспорт"
            try:
                # Используем JavaScript для мгновенного нахождения и нажатия кнопки экспорта
                driver.execute_script("""
                    var exportBtn = document.querySelector('button.export-make');
                    if (exportBtn) exportBtn.click();
                """)
                logger.info("Нажата кнопка 'Экспорт'")
            except Exception:
                # Запасной вариант, если JavaScript не сработал
                try:
                    export_make_button = WebDriverWait(driver, 5).until(
                        EC.element_to_be_clickable((By.CSS_SELECTOR, "button.export-make"))
                    )
                    export_make_button.click()
                    logger.info("Нажата кнопка 'Экспорт' через Selenium")
                except Exception as e:
                    logger.error(f"Не удалось нажать кнопку 'Экспорт': {e}")
                    return False
            
            # Динамическое ожидание появления кнопки "Скачать" после нажатия на "Экспорт"
            start_time = time.time()
            max_wait_time = 300  # максимальное время ожидания в секундах
            check_interval = 0.5  # проверяем каждые 0.5 секунды
            
            while time.time() - start_time < max_wait_time:
                try:
                    # Используем JavaScript для быстрой проверки наличия кнопки
                    is_button_ready = driver.execute_script("""
                        var btn = document.querySelector('button.export-download');
                        return btn && btn.offsetParent !== null && !btn.disabled;
                    """)
                    
                    if is_button_ready:
                        download_button = driver.find_element(By.CSS_SELECTOR, "button.export-download")
                        logger.info("Кнопка 'Скачать' найдена после нажатия 'Экспорт'")
                        break
                except Exception:
                    pass
                time.sleep(check_interval)
            
            if not download_button:
                logger.error("Кнопка 'Скачать' не появилась после нажатия на 'Экспорт'")
                return False
        
        # Нажатие на кнопку "Скачать"
        logger.info("Нажатие на кнопку 'Скачать'")
        try:
            # Используем JavaScript для более быстрого и надежного клика
            driver.execute_script("arguments[0].click();", download_button)
            logger.info("Кнопка 'Скачать' успешно нажата")
        except Exception as e:
            logger.error(f"Ошибка при нажатии на кнопку 'Скачать': {e}")
            return False
        
        # Ожидание загрузки файла 
        logger.info("Ожидание завершения скачивания файла")
        new_files = wait_for_download_complete(timeout=120)  # 2 минуты на загрузку
        
        if new_files:
            logger.info(f"Результат скачивания: успешно. Найдены файлы: {new_files}")
            # Находим PDF файл среди новых файлов
            pdf_files = [f for f in new_files if f.lower().endswith('.pdf')]
            
            if pdf_files:
                latest_pdf = pdf_files[0]
                
                # Формирование нового имени файла с санитизацией
                sanitized_title = sanitize_filename(document_title)
                logger.info(f"Санитизированное название: {sanitized_title}")
                new_filename = f"{sanitized_title}.pdf"
                old_path = os.path.join(DOWNLOAD_DIR, latest_pdf)
                new_path = os.path.join(DOWNLOAD_DIR, new_filename)
                
                # Переименование файла
                logger.info(f"Переименовывание файла из '{latest_pdf}' в '{new_filename}'")
                try:
                    os.rename(old_path, new_path)
                    logger.info(f"Название сохранённого файла: {new_filename}")
                    logger.info(f"Путь к файлу: {new_path}")
                except FileExistsError:
                    # Если файл с таким именем уже существует, добавляем временную метку
                    timestamp = int(time.time())
                    new_filename = f"{sanitized_title}_{timestamp}.pdf"
                    new_path = os.path.join(DOWNLOAD_DIR, new_filename)
                    logger.info(f"Файл уже существует, новое имя: {new_filename}")
                    os.rename(old_path, new_path)
                    logger.info(f"Название сохранённого файла: {new_filename}")
                    logger.info(f"Путь к файлу: {new_path}")
                except Exception as e:
                    logger.error(f"Ошибка при переименовании файла: {e}")
                    logger.info(f"Файл скачан, но не переименован: {old_path}")
                    return True
                
                logger.info(f"Результат сохранения отчёта и обработки ссылки: успешно")
                return True
            else:
                logger.warning(f"PDF файл не найден среди загруженных файлов: {new_files}")
        else:
            logger.error(f"Результат скачивания: не удалось скачать файл")
        
        # Если файл не был загружен, повторяем попытку
        logger.warning(f"Попытка {retry_count+1} не удалась, повторная попытка...")
        time.sleep(RETRY_DELAY)  # Небольшая пауза перед повторной попыткой
        return download_report(driver, url, retry_count + 1)
    
    except Exception as e:
        logger.error(f"Ошибка при обработке ссылки {url}: {str(e)}")
        # Повторная попытка при ошибке
        time.sleep(RETRY_DELAY)  # Небольшая пауза перед повторной попыткой
        return download_report(driver, url, retry_count + 1)

async def is_valid_url(url):
    """Проверка валидности URL"""
    url_pattern = re.compile(
        r'^(?:http|https)://'
        r'(?:(?:[A-Z0-9](?:[A-Z0-9-]{0,61}[A-Z0-9])?\.)+(?:[A-Z]{2,6}\.?|[A-Z0-9-]{2,}\.?)|'
        r'localhost|'
        r'\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3})'
        r'(?::\d+)?'
        r'(?:/?|[/?]\S+)$', re.IGNORECASE)
    return bool(url_pattern.match(url))

async def get_bot_links(client, bot_username, target_message_ids=None):
    """Получение всех ссылок из кнопок в чате с ботом"""
    logger.info(f"Получение ссылок из чата с ботом {bot_username}")
    
    try:
        # Получение диалога с ботом
        entity = await client.get_entity(bot_username)
        
        all_links = []
        
        # Если заданы конкретные ID сообщений
        if target_message_ids and target_message_ids:
            messages = await client.get_messages(entity, ids=target_message_ids)
            
            for message in messages:
                # Проверка наличия кнопок в сообщении
                if message.reply_markup:
                    for row in message.reply_markup.rows:
                        for button in row.buttons:
                            if hasattr(button, 'url') and button.url:
                                # Проверка, содержит ли текст кнопки "Посмотреть отчёт" (регистронезависимо)
                                button_text = button.text.lower()
                                if "посмотреть отчёт" in button_text or "посмотреть отчет" in button_text:
                                    # Проверка валидности URL
                                    if await is_valid_url(button.url):
                                        all_links.append(button.url)
        else:
            # Стандартная логика получения всех ссылок
            offset_id = 0
            limit = 10000
            total_messages = 0
            
            while True:
                messages = await client.get_messages(entity, limit=limit, offset_id=offset_id)
                if not messages:
                    break
                
                total_messages += len(messages)
                
                for message in messages:
                    # Проверка наличия кнопок в сообщении
                    if message.reply_markup:
                        for row in message.reply_markup.rows:
                            for button in row.buttons:
                                if hasattr(button, 'url') and button.url:
                                    # Проверка, содержит ли текст кнопки "Посмотреть отчёт" (регистронезависимо)
                                    button_text = button.text.lower()
                                    if "посмотреть отчёт" in button_text or "посмотреть отчет" in button_text:
                                        # Проверка валидности URL
                                        if await is_valid_url(button.url):
                                            all_links.append(button.url)
                
                # Если сообщений меньше, чем лимит, значит достигнут конец истории
                if len(messages) < limit:
                    break
                
                # Обновление смещения для следующей порции сообщений
                offset_id = messages[-1].id
                
                # Небольшая пауза, чтобы не перегружать API
                await asyncio.sleep(0.2)
        
        logger.info(f"Всего найдено {len(all_links)} ссылок в кнопках")
        return all_links
    except Exception as e:
        logger.error(f"Ошибка при получении ссылок из чата с ботом: {e}")
        return []

async def check_session_exists(phone_number=None):
    """Проверка существования файла сессии"""
    # Проверяем существование любой сессии с суффиксом _file_downloader
    session_files = [f for f in os.listdir() if f.endswith('_file_downloader.session')]
    
    if session_files:
        # Возвращаем имя первого найденного файла сессии без расширения
        return session_files[0].replace('.session', '')
    
    # Если номер телефона указан явно, проверяем его сессию
    if phone_number:
        session_file = f"{phone_number.replace('+', '')}_file_downloader.session"
        if os.path.exists(session_file):
            return phone_number.replace('+', '') + "_file_downloader"
    
    return None

async def get_session_name(phone_number):
    """Получает имя файла сессии на основе номера телефона"""
    return f"{phone_number.replace('+', '')}_file_downloader"

async def login_telegram(api_id, api_hash):
    """Выполняет вход в Telegram с использованием существующей сессии или создаёт новую"""
    try:
        # Проверяем наличие существующей сессии
        session_name = await check_session_exists()
        
        if session_name:
            logger.info(f"Найдена существующая сессия: {session_name}")
            client = TelegramClient(session_name, api_id, api_hash)
            await client.start()
            
            if await client.is_user_authorized():
                logger.info("Успешная авторизация по существующей сессии")
                return client
            else:
                logger.warning("Существующая сессия недействительна, требуется повторная авторизация")
        
        # Создаём новую сессию, если существующая не найдена или недействительна
        phone = input("Введите номер телефона в международном формате (с +): ")
        if not phone.startswith('+'):
            logger.error("Номер телефона должен начинаться с +")
            return None
            
        clean_phone = phone.replace('+', '')
        session_name = f"{clean_phone}_file_downloader"
        
        logger.info(f"Создание новой сессии для номера {phone}")
        client = TelegramClient(session_name, api_id, api_hash)
        
        await client.start(phone=phone)
        
        if not await client.is_user_authorized():
            logger.error("Не удалось авторизоваться")
            return None
        
        logger.info("Успешная авторизация с новой сессией")
        return client
    except Exception as e:
        logger.error(f"Ошибка при входе в Telegram: {e}")
        return None

async def process_files_from_user(client, user_id, bot_username, db, ignore_saved_state=False):
    """Обрабатывает файлы от пользователя и отправляет их боту"""
    try:
        # Получаем информацию о пользователе
        user_entity = await client.get_entity(user_id)
        
        # Если игнорируем сохраненное состояние, очищаем состояние процесса и pending файлы
        if ignore_saved_state:
            db.clear_user_process_state(str(user_entity.id))
            db.clear_pending_files(str(user_entity.id))
        
        # Пересылаем файлы боту
        processed_count = await forward_files_to_bot(client, user_id, bot_username, db)
        
        if processed_count == 0:
            # Если нет файлов для обработки, очищаем состояние процесса
            db.clear_user_process_state(str(user_entity.id))
            return []
        
        # Мониторим ответы бота
        successful_response_ids = await monitor_bot_responses(client, bot_username, user_id, db)
        
        # Если нет успешных ответов, очищаем состояние процесса
        if not successful_response_ids:
            db.clear_user_process_state(str(user_entity.id))
        
        return successful_response_ids
    except Exception as e:
        logger.error(f"Ошибка в process_files_from_user: {e}")
        # Очищаем состояние в случае ошибки
        db.clear_user_process_state(str(user_entity.id))
        return []

async def continue_process_from_state(client, user_id, state, data, db):
    """Продолжает процесс с сохраненного состояния"""
    logger.info(f"Продолжение процесса с состояния: {state}")
    
    try:
        user_entity = await client.get_entity(user_id)
        
        # Определяем, на каком этапе был прерван процесс
        if state == "forwarding_files" or state == "forwarding_files_batch":
            # Начинаем или продолжаем пересылку файлов
            logger.info(f"Продолжение с этапа пересылки файлов для пользователя {user_id}")
            # Полностью повторяем процесс пересылки, так как трудно восстановить точное место остановки
            processed_count = await forward_files_to_bot(client, user_id, BOT_USERNAME, db)
            
            if processed_count == 0:
                # Если нет файлов для пересылки, возможно все уже пересланы
                logger.info("Нет новых файлов для пересылки. Переходим к мониторингу ответов.")
                
                # Проверяем, есть ли pending файлы в БД
                pending_files = db.get_pending_files(str(user_entity.id))
                logger.info(f"Найдено {len(pending_files)} файлов в статусе 'pending' в БД")
                
                if pending_files:
                    # Есть файлы, ожидающие ответа от бота - продолжаем мониторинг
                    logger.info("Есть файлы в статусе 'pending', продолжаем мониторинг ответов бота")
                    successful_responses = await monitor_bot_responses(client, BOT_USERNAME, user_id, db, data)
                else:
                    # Нет pending файлов, возможно все уже обработаны
                    # Получаем успешные ответы из БД
                    logger.info("Нет файлов в статусе 'pending', проверяем существующие успешные ответы")
                    successful_responses = db.get_successful_responses(str(user_entity.id))
                    logger.info(f"Найдено {len(successful_responses)} успешных ответов в БД")
                    
                    if not successful_responses:
                        logger.warning("Не найдено ни pending файлов, ни успешных ответов")
                        # Очищаем состояние - нечего обрабатывать
                        db.clear_user_process_state(str(user_entity.id))
                        return False
                
                # Если есть успешные ответы, продолжаем с загрузки отчетов
                if successful_responses:
                    # Далее скачиваем отчеты
                    logger.info(f"Переходим к скачиванию отчетов для {len(successful_responses)} успешных ответов")
                    report_paths = await download_reports_mode(client, successful_responses)
                    
                    if report_paths:
                        # И отправляем их пользователю
                        await send_reports_to_user(client, user_id, report_paths)
                        # Очищаем состояние - процесс завершен
                        db.clear_user_process_state(str(user_entity.id))
                        return True
                    else:
                        logger.warning("Не удалось скачать отчеты")
                        return False
                else:
                    logger.warning("Нет успешных ответов от бота")
                    return False
            else:
                # Если есть файлы для пересылки, далее мониторим ответы
                successful_responses = await monitor_bot_responses(client, BOT_USERNAME, user_id, db)
                
                if successful_responses:
                    # После получения ответов скачиваем отчеты
                    report_paths = await download_reports_mode(client, successful_responses)
                    
                    if report_paths:
                        # И отправляем их пользователю
                        await send_reports_to_user(client, user_id, report_paths)
                        # Очищаем состояние - процесс завершен
                        db.clear_user_process_state(str(user_entity.id))
                        return True
                    else:
                        logger.warning("Не удалось скачать отчеты")
                        return False
                else:
                    logger.warning("Нет успешных ответов от бота")
                    return False
                    
        elif state == "files_forwarded" or state == "monitoring_responses" or state == "monitoring_responses_progress":
            # Продолжаем с мониторинга ответов бота
            logger.info(f"Продолжение с этапа мониторинга ответов бота для пользователя {user_id}")
            
            # Сначала проверяем, есть ли данные для восстановления
            if data and 'pending_message_ids' in data and data['pending_message_ids']:
                # Продолжаем мониторинг с имеющимися данными
                logger.info(f"Восстанавливаем мониторинг с {len(data['pending_message_ids'])} pending сообщений")
                successful_responses = await monitor_bot_responses(client, BOT_USERNAME, user_id, db, data)
            else:
                # Проверяем БД на наличие pending файлов
                pending_files = db.get_pending_files(str(user_entity.id))
                logger.info(f"Найдено {len(pending_files)} файлов в статусе 'pending' в БД")
                
                if pending_files:
                    # Есть pending файлы - запускаем мониторинг
                    successful_responses = await monitor_bot_responses(client, BOT_USERNAME, user_id, db)
                else:
                    # Нет pending файлов - возможно все уже обработано
                    logger.info("Нет pending файлов, проверяем успешные ответы")
                    successful_responses = db.get_successful_responses(str(user_entity.id))
                    logger.info(f"Найдено {len(successful_responses)} успешных ответов в БД")
            
            if successful_responses:
                # Переходим к скачиванию отчетов
                report_paths = await download_reports_mode(client, successful_responses)
                
                if report_paths:
                    # Отправляем скачанные отчеты пользователю
                    await send_reports_to_user(client, user_id, report_paths)
                    # Очищаем состояние процесса - все завершено
                    db.clear_user_process_state(str(user_entity.id))
                    return True
                else:
                    logger.warning("Не удалось скачать отчеты")
                    return False
            else:
                logger.warning("Нет успешных ответов от бота для скачивания отчетов")
                return False
        
        elif state == "responses_monitored" or state == "downloading_reports":
            # Продолжаем со скачивания отчетов
            logger.info(f"Продолжение с этапа скачивания отчетов для пользователя {user_id}")
            
            successful_responses = data.get("successful_responses", []) if data else []
            if not successful_responses:
                logger.warning("Нет данных об успешных ответах в сохраненном состоянии")
                
                # Пробуем получить успешные ответы из базы данных
                successful_responses = db.get_successful_responses(str(user_entity.id))
                logger.info(f"Получено {len(successful_responses)} успешных ответов из БД")
                if not successful_responses:
                    logger.warning("Не удалось найти информацию о успешных ответах")
                    return False
            
            # Скачиваем отчеты
            report_paths = await download_reports_mode(client, successful_responses)
            
            if report_paths:
                # Отправляем скачанные отчеты пользователю
                await send_reports_to_user(client, user_id, report_paths)
                # Очищаем состояние процесса - все завершено
                db.clear_user_process_state(str(user_entity.id))
                return True
            else:
                logger.warning("Не удалось скачать отчеты")
                return False
        
        elif state == "reports_downloaded" or state == "sending_reports":
            # Продолжаем с отправки отчетов пользователю
            logger.info(f"Продолжение с этапа отправки отчетов пользователю {user_id}")
            
            # Получаем пути к отчетам из данных состояния
            report_paths = data.get("report_paths", []) if data else []
            
            if not report_paths:
                logger.warning("Нет данных о путях к отчетам в сохраненном состоянии")
                
                # Пробуем найти все PDF файлы в директории загрузок
                report_paths = [os.path.join(DOWNLOAD_DIR, f) for f in os.listdir(DOWNLOAD_DIR) 
                               if os.path.isfile(os.path.join(DOWNLOAD_DIR, f)) and f.lower().endswith('.pdf')]
                
                if not report_paths:
                    logger.warning("Не удалось найти отчеты для отправки")
                    return False
            
            # Отправляем отчеты
            await send_reports_to_user(client, user_id, report_paths)
            
            # Очищаем состояние процесса - все завершено
            db.clear_user_process_state(str(user_entity.id))
            return True
        
        else:
            logger.warning(f"Неизвестное состояние процесса: {state}")
            return False
            
        return False  # Если ни одно из условий не сработало
    
    except Exception as e:
        logger.error(f"Ошибка при продолжении процесса с состояния {state}: {e}")
        return False

async def download_reports_mode(client, target_message_ids=None):
    """Режим только скачивания отчетов"""
    driver = None
    report_paths = []
    
    try:
        # Получение всех ссылок из кнопок в чате с ботом
        all_links = await get_bot_links(client, BOT_USERNAME, target_message_ids)
        
        if not all_links:
            logger.info("Не найдено ссылок для обработки")
            return []
        
        # Получаем список уже обработанных ссылок
        processed_links = get_processed_links()
        
        # Определяем новые ссылки для обработки
        links_to_process = [link for link in all_links if link not in processed_links]
        
        if not links_to_process:
            logger.info("Все найденные ссылки уже обработаны")
            return []
            
        logger.info(f"Найдено {len(links_to_process)} новых ссылок для обработки")
        
        # Инициализация веб-драйвера
        driver = setup_driver()
        
        # Обработка каждой ссылки
        total_links = len(links_to_process)
        processed_count = 0
        
        for i, link in enumerate(links_to_process, 1):
            try:
                logger.info(f"Обработка ссылки {i}/{total_links}")
                
                # Скачивание отчета с поддержкой повторных попыток
                success = download_report(driver, link)
                
                if success:
                    # Сохранение обработанной ссылки
                    save_processed_link(link)
                    processed_count += 1
                    logger.info(f"Обработано ссылок: {processed_count}/{total_links}")
                    
                    # Находим путь к последнему скачанному файлу
                    latest_files = [os.path.join(DOWNLOAD_DIR, f) for f in os.listdir(DOWNLOAD_DIR) 
                                   if os.path.isfile(os.path.join(DOWNLOAD_DIR, f)) and f.endswith('.pdf')]
                    if latest_files:
                        latest_file = max(latest_files, key=os.path.getctime)
                        report_paths.append(latest_file)
                else:
                    logger.error(f"Результат обработки ссылки {link}: не удалось обработать после всех попыток")
            except Exception as e:
                logger.error(f"Ошибка при обработке ссылки {link}: {e}")
                continue
        
        logger.info(f"Обработка завершена. Обработано {processed_count} из {total_links} ссылок")
        return report_paths
    
    except Exception as e:
        logger.error(f"Ошибка при скачивании отчетов: {e}")
        return []
    
    finally:
        # Закрытие веб-драйвера
        if driver:
            try:
                driver.quit()
            except Exception:
                pass

async def file_forwarding_mode(client, db):
    """Режим пересылки файлов и скачивания отчетов"""
    try:
        # Запрос имени пользователя
        username = input("Введите имя пользователя (с @): ")
        if not username.startswith('@'):
            username = '@' + username
        
        try:
            # Получаем информацию о пользователе
            user_entity = await client.get_entity(username)
            user_id = str(user_entity.id)
            
            # Проверяем, есть ли незавершенный процесс для этого пользователя
            state, data = db.get_user_process_state(user_id)
            
            if state:
                print(f"Для данного пользователя найден незавершённый процесс. Хотите продолжить с места остановки? Введите только да/нет.")
                choice = input().strip().lower()
                
                if choice == "да":
                    # Продолжаем процесс с места остановки
                    logger.info(f"Продолжение процесса для пользователя {username} с состояния {state}")
                    success = await continue_process_from_state(client, username, state, data, db)
                    if success:
                        logger.info(f"Процесс для пользователя {username} успешно завершен")
                    else:
                        logger.warning(f"Не удалось завершить процесс для пользователя {username}")
                    return
                elif choice == "нет":
                    # Очищаем состояние процесса и записи 'pending' в processed_files
                    logger.info(f"Начало нового процесса для пользователя {username}")
                    db.clear_user_process_state(user_id)
                    db.clear_pending_files(user_id)  # Очищаем только processed_files
                else:
                    logger.error("Неверный ввод. Пожалуйста, введите 'да' или 'нет'")
                    return
        except Exception as e:
            logger.error(f"Ошибка при проверке состояния для пользователя {username}: {e}")
        
        # Начинаем новый процесс
        logger.info(f"Запуск нового процесса для пользователя {username}")
        successful_responses = await process_files_from_user(client, username, BOT_USERNAME, db, ignore_saved_state=True)
        
        if not successful_responses:
            logger.info("Нет успешно обработанных файлов для скачивания отчетов")
            try:
                user_entity = await client.get_entity(username)
                db.clear_user_process_state(str(user_entity.id))
            except Exception as e:
                logger.error(f"Ошибка при очистке состояния: {e}")
            return
        
        # Запускаем скачивание отчетов
        logger.info("Запуск скачивания отчетов...")
        
        # Сохраняем состояние - начало скачивания отчетов
        try:
            user_entity = await client.get_entity(username)
            db.save_user_process_state(str(user_entity.id), "downloading_reports", {
                "successful_responses": successful_responses
            })
        except Exception as e:
            logger.error(f"Ошибка при сохранении состояния: {e}")
        
        # Скачиваем отчеты
        report_paths = await download_reports_mode(client, successful_responses)
        
        if report_paths:
            # Сохраняем состояние - начало отправки отчетов
            try:
                user_entity = await client.get_entity(username)
                db.save_user_process_state(str(user_entity.id), "sending_reports", {
                    "report_paths": report_paths
                })
            except Exception as e:
                logger.error(f"Ошибка при сохранении состояния: {e}")
            
            # Отправляем скачанные отчеты обратно пользователю
            await send_reports_to_user(client, username, report_paths)
            
            # Очищаем состояние - все завершено успешно
            try:
                user_entity = await client.get_entity(username)
                db.clear_user_process_state(str(user_entity.id))
            except Exception as e:
                logger.error(f"Ошибка при очистке состояния: {e}")
                
            logger.info(f"Все отчеты успешно отправлены пользователю {username}")
        else:
            logger.warning("Не удалось скачать отчеты")
            # Очищаем состояние, так как процесс завершился неудачей
            try:
                user_entity = await client.get_entity(username)
                db.clear_user_process_state(str(user_entity.id))
            except Exception as e:
                logger.error(f"Ошибка при очистке состояния: {e}")
    
    except Exception as e:
        logger.error(f"Ошибка в режиме пересылки файлов: {e}")
        try:
            user_entity = await client.get_entity(username)
            db.clear_user_process_state(str(user_entity.id))
        except Exception as e2:
            logger.error(f"Ошибка при очистке состояния: {e2}")

async def main():
    """Основная функция скрипта"""
    try:
        # Загрузка API данных
        api_id, api_hash = load_api_config()
        
        # Создание клиента Telegram и вход по существующей сессии или с новой авторизацией
        client = await login_telegram(api_id, api_hash)
        
        if not client:
            logger.error("Не удалось создать клиент Telegram")
            return
        
        # Инициализация базы данных
        db = Database(DB_FILE)
        
        try:
            # Выбор режима работы
            print("Выберите действие и введите его номер:")
            print("1. Только скачать отчёты по ссылкам.")
            print("2. Переслать файлы в бота, скачать отчёты, отправить клиенту.")
            
            choice = input("Ваш выбор: ")
            
            if choice == "1":
                # Режим скачивания отчетов
                await download_reports_mode(client)
            elif choice == "2":
                # Режим пересылки файлов, скачивания отчетов и отправки клиенту
                await file_forwarding_mode(client, db)
            else:
                logger.error("Неверный выбор. Пожалуйста, выберите 1 или 2.")
        
        finally:
            # Закрытие клиента Telegram
            if client:
                await client.disconnect()
                logger.info("Клиент Telegram отключен")
    
    except KeyboardInterrupt:
        logger.info("Скрипт остановлен пользователем (Ctrl+C)")
    except Exception as e:
        logger.error(f"Произошла непредвиденная ошибка: {e}")

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Скрипт остановлен пользователем (Ctrl+C)")
        sys.exit(0)
    except Exception as e:
        logger.critical(f"Критическая ошибка при выполнении скрипта: {e}")
        sys.exit(1)