from telethon import TelegramClient, events, Button
import os
import time
import asyncio
import sqlite3
from dotenv import load_dotenv
from telethon.errors import FloodWaitError

# Carregar variáveis do arquivo .env
load_dotenv()

# Obtendo as variáveis do arquivo .env
api_id = os.getenv('API_ID')
api_hash = os.getenv('API_HASH')
bot_token = os.getenv('BOT_TOKEN')

# Inicializando o cliente do bot
bot = TelegramClient('bot', api_id, api_hash).start(bot_token=bot_token)

# Criar conexão com o banco de dados SQLite
conn = sqlite3.connect('downloads.db')
cursor = conn.cursor()

# Criar tabela de downloads se não existir
cursor.execute('''
    CREATE TABLE IF NOT EXISTS downloads (
        id INTEGER PRIMARY KEY,
        file_id TEXT,
        file_name TEXT,
        file_path TEXT,
        status TEXT
    )
''')
conn.commit()

# Criando uma fila para gerenciar os downloads
video_queue = asyncio.Queue()
is_downloading = False
download_status = {}
completed_downloads = []
failed_downloads = []

# Sinalizador para pausar downloads
pause_event = asyncio.Event()
pause_event.set()  # Começamos sem pausas

# Função para adicionar um download no banco de dados
def add_download_to_db(file_id, file_name, status="pending"):
    cursor.execute('''
        INSERT INTO downloads (file_id, file_name, status) VALUES (?, ?, ?)
    ''', (file_id, file_name, status))
    conn.commit()

# Função para atualizar o status do download no banco de dados
def update_download_status(file_id, status, file_path=None):
    cursor.execute('''
        UPDATE downloads SET status = ?, file_path = ? WHERE file_id = ?
    ''', (status, file_path, file_id))
    conn.commit()

# Verifica se o download já foi feito antes de processar
def is_download_completed(file_id):
    cursor.execute('SELECT status, file_path FROM downloads WHERE file_id = ?', (file_id,))
    result = cursor.fetchone()

    # Verificar se o arquivo foi baixado e ainda existe
    if result:
        status, file_path = result
        if status == 'completed' and os.path.exists(file_path):
            return True  # O arquivo já foi baixado e ainda existe
        elif status == 'completed' and not os.path.exists(file_path):
            # Arquivo não existe mais, então marca como pendente novamente
            update_download_status(file_id, 'pending')
            return False
        else:
            return False  # O arquivo ainda está pendente ou em andamento
    return False  # O download não foi registrado no banco de dados ainda

# Função para monitorar e exibir o progresso do download
async def progress_callback(downloaded, total, status_message, start_time, video_name):
    while not pause_event.is_set():
        await asyncio.sleep(1)  # Pausar se o sinalizador estiver desligado

    elapsed_time = time.time() - start_time
    percent_completed = downloaded / total * 100
    speed = downloaded / elapsed_time if elapsed_time > 0 else 0
    estimated_total_time = total / speed if speed > 0 else 0
    estimated_time_remaining = estimated_total_time - elapsed_time

    download_status[video_name] = {
        "percent": percent_completed,
        "remaining_time": estimated_time_remaining
    }

    # Atualize a mensagem de progresso apenas a cada 5% de progresso
    if int(percent_completed) % 5 == 0:
        if status_message:
            try:
                await status_message.edit(f"Baixando {video_name}...\n"
                                          f"Progresso: {percent_completed:.2f}%\n"
                                          f"Tempo restante: {estimated_time_remaining:.2f} segundos")
            except FloodWaitError as fwe:
                print(f"FloodWaitError: Aguardando {fwe.seconds} segundos antes de tentar novamente.")
                await asyncio.sleep(fwe.seconds)

# Função para processar o download de um vídeo
async def process_video_download(file_id, video_name, status_message, event):
    global is_downloading
    try:
        is_downloading = True
        download_dir = './downloads'
        if not os.path.exists(download_dir):
            os.makedirs(download_dir)

        start_time = time.time()

        # Atualizar o status de início no banco de dados
        update_download_status(file_id, "downloading")

        # Fazendo o download real do vídeo usando download_media
        file_path = await event.message.download_media(
            file=download_dir,
            progress_callback=lambda d, t: progress_callback(d, t, status_message, start_time, video_name)
        )

        # Verifica se o arquivo foi baixado corretamente
        if os.path.exists(file_path):
            completed_downloads.append(file_path)
            update_download_status(file_id, 'completed', file_path)

            if status_message:
                try:
                    await status_message.edit(f"Download concluído! Vídeo salvo em {file_path}")
                except FloodWaitError as fwe:
                    print(f"FloodWaitError: Aguardando {fwe.seconds} segundos.")
                    await asyncio.sleep(fwe.seconds)
                    await status_message.edit(f"Download concluído! Vídeo salvo em {file_path}")
        else:
            raise Exception("Falha no download, arquivo não encontrado.")

    except FloodWaitError as fwe:
        print(f"Aguardando {fwe.seconds} segundos devido ao FloodWaitError.")
        await asyncio.sleep(fwe.seconds)

    except Exception as e:
        failed_downloads.append(video_name)
        update_download_status(file_id, 'failed')
        if status_message:
            try:
                await status_message.reply(f"Ocorreu um erro durante o download de {video_name}: {str(e)}")
            except FloodWaitError as fwe:
                print(f"Aguardando {fwe.seconds} segundos devido ao FloodWaitError.")
                await asyncio.sleep(fwe.seconds)

    finally:
        is_downloading = False
        video_queue.task_done()

# Função para processar a fila de vídeos
async def process_queue():
    global is_downloading
    while not video_queue.empty():
        download_info = await video_queue.get()

        if isinstance(download_info, dict):
            file_id = download_info['file_id']
            video_name = download_info['file_name']
            event = download_info['event']

            # Criar uma mensagem de progresso
            status_message = await event.reply(f"Iniciando o download de {video_name}...")

            # Inicia o processo de download
            await process_video_download(file_id, video_name, status_message, event)
        else:
            event = download_info
            video_name = event.message.file.name if event.message.file else "vídeo desconhecido"
            file_id = event.message.id

            # Verifica se o download já foi concluído
            if is_download_completed(file_id):
                await event.reply(f"O download de {video_name} já foi concluído anteriormente.")
            else:
                # Adicionar ao banco de dados se não existir ainda
                add_download_to_db(file_id, video_name)

                # Criar uma mensagem de progresso
                status_message = await event.reply(f"Iniciando o download de {video_name}...")

                await process_video_download(file_id, video_name, status_message, event)

# Evento que recebe vídeos
@bot.on(events.NewMessage(incoming=True, func=lambda e: e.video))
async def handle_video(event):
    global is_downloading

    # Adiciona o vídeo à fila de downloads
    await video_queue.put({'file_id': event.message.id, 'file_name': event.message.file.name, 'event': event})
    await event.reply("Seu vídeo foi adicionado à fila de downloads.")

    if not is_downloading:
        await process_queue()

# Função para recuperar downloads pendentes do banco de dados
async def recover_pending_downloads():
    cursor.execute('SELECT file_id, file_name FROM downloads WHERE status = ?', ('pending',))
    pending_downloads = cursor.fetchall()

    for file_id, file_name in pending_downloads:
        print(f"Recuperando download pendente: {file_name}")
        await video_queue.put({'file_id': file_id, 'file_name': file_name})
    
    if not video_queue.empty():
        await process_queue()

# Comando para verificar o status dos downloads
@bot.on(events.NewMessage(pattern='/status'))
async def check_status(event):
    status_message = "📊 **Status de Downloads** 📊\n\n"

    if is_downloading:
        status_message += "⏳ Download em andamento:\n"
        for video, status in download_status.items():
            status_message += f"- {video}: {status['percent']:.2f}% concluído, faltam {status['remaining_time']:.2f} segundos\n"
    else:
        status_message += "✅ Nenhum download em andamento no momento.\n"

    if not video_queue.empty():
        status_message += "\n🎬 Vídeos na fila:\n"
        status_message += f"{video_queue.qsize()} vídeo(s) aguardando na fila.\n"
    else:
        status_message += "\n📭 Fila de downloads está vazia.\n"

    if completed_downloads:
        status_message += "\n✅ **Downloads Concluídos**:\n"
        for video in completed_downloads:
            status_message += f"- {video}\n"

    if failed_downloads:
        status_message += "\n❌ **Falhas no Download**:\n"
        for video in failed_downloads:
            status_message += f"- {video}\n"

    await event.reply(status_message)

# Comando para pausar o download
@bot.on(events.NewMessage(pattern='/pausar'))
async def pause_download(event):
    pause_event.clear()  # Pausa o processo de download
    await event.reply("⏸️ O download foi pausado.")

# Comando para continuar o download
@bot.on(events.NewMessage(pattern='/continuar'))
async def continue_download(event):
    pause_event.set()  # Continua o processo de download
    await event.reply("▶️ O download foi retomado.")

# Comando para cancelar todos os downloads
@bot.on(events.NewMessage(pattern='/cancelar'))
async def cancel_downloads(event):
    global video_queue
    video_queue = asyncio.Queue()  # Esvazia a fila de downloads
    pause_event.set()  # Libera qualquer pausa pendente
    await event.reply("🚫 Todos os downloads foram cancelados.")

# Comando para exibir o menu de opções
@bot.on(events.NewMessage(pattern='/menu'))
async def show_menu(event):
    buttons = [
        [Button.text("📊 Ver Status", resize=True, single_use=True)],
        [Button.text("⏸️ Pausar Download"), Button.text("▶️ Continuar Download")],
        [Button.text("🚫 Cancelar Todos")]
    ]

    await event.reply("📋 **Menu de Opções** 📋\nEscolha uma opção:", buttons=buttons)

# Gerenciando interações com os botões do menu
@bot.on(events.NewMessage(func=lambda e: e.message.message in ["📊 Ver Status", "⏸️ Pausar Download", "▶️ Continuar Download", "🚫 Cancelar Todos"]))
async def handle_menu_selection(event):
    message = event.message.message
    if message == "📊 Ver Status":
        await check_status(event)
    elif message == "⏸️ Pausar Download":
        await pause_download(event)
    elif message == "▶️ Continuar Download":
        await continue_download(event)
    elif message == "🚫 Cancelar Todos":
        await cancel_downloads(event)

# Iniciando o bot e recuperando downloads pendentes
async def main():
    await recover_pending_downloads()  # Recupera downloads pendentes ao iniciar o bot
    await bot.run_until_disconnected()  # Aguarda o bot desconectar

if __name__ == "__main__":
    loop = asyncio.get_event_loop()
    loop.run_until_complete(main())
