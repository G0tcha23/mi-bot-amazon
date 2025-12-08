import logging
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, ContextTypes, filters, ConversationHandler
import requests
from bs4 import BeautifulSoup
import urllib.parse
import random
import json
import os
import threading
import http.server
import socketserver

# Configuración de logging
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

# Token del bot
TOKEN = "5542545245:AAETXpqyI9htrp760FqZ1TmI7N0A4zKhY1I"

# Configuración de Grupos
GRUPOS_CONFIG = {
    -1001593262082: "Rockstar Store",
    -1001521082473: "Rockstar Spain",
    -1001461104385: "Spain Game"
}

# Estados de la conversación
# Orden cambiado: REVIEW va antes de OFERTA
ESPERANDO_ASIN, SELECCIONANDO_REVIEW, SELECCIONANDO_OFERTA, ESPERANDO_TEXTO_EUROS, SELECCIONANDO_TIPO_ENLACE, ESPERANDO_KEYWORD, SELECCIONANDO_GRUPOS, POST_PUBLICACION = range(8)

# Datos temporales del usuario
user_data_storage = {}

# Lista rotativa de User-Agents para evitar bloqueos
USER_AGENTS = [
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.2 Safari/605.1.15',
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:109.0) Gecko/20100101 Firefox/121.0',
    'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36'
]

def get_headers():
    return {
        'User-Agent': random.choice(USER_AGENTS),
        'Accept-Language': 'es-ES,es;q=0.9,en;q=0.8',
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,image/apng,*/*;q=0.8',
        'Referer': 'https://www.amazon.es/'
    }

# --- SERVIDOR WEB DUMMY PARA RENDER ---
def run_dummy_server():
    """Ejecuta un servidor web simple para engañar a Render y que no cierre el bot"""
    PORT = int(os.environ.get("PORT", 8080))
    Handler = http.server.SimpleHTTPRequestHandler
    
    # Clase personalizada para responder 200 OK a todo (Health Check)
    class HealthCheckHandler(Handler):
        def do_GET(self):
            self.send_response(200)
            self.send_header('Content-type', 'text/html')
            self.end_headers()
            self.wfile.write(b"Bot is alive!")

    with socketserver.TCPServer(("", PORT), HealthCheckHandler) as httpd:
        logger.info(f"🌍 Dummy server corriendo en puerto {PORT}")
        httpd.serve_forever()

def start_server_thread():
    thread = threading.Thread(target=run_dummy_server)
    thread.daemon = True
    thread.start()

# --- SISTEMA DE HISTORIAL ---
HISTORY_FILE = 'historial.json'

def load_history():
    """Carga el historial desde el archivo JSON de forma segura"""
    if not os.path.exists(HISTORY_FILE):
        return []
    try:
        with open(HISTORY_FILE, 'r', encoding='utf-8') as f:
            content = f.read().strip()
            if not content: return []
            return json.loads(content)
    except Exception as e:
        logger.error(f"Error cargando historial: {e}")
        return []

def save_history(new_item):
    """Guarda un nuevo item en el historial (máximo 20 items)"""
    history = load_history()
    # Eliminar si ya existe para ponerlo el primero (evitar duplicados)
    history = [item for item in history if item['asin'] != new_item['asin']]
    history.insert(0, new_item)
    # Mantener solo los últimos 20
    history = history[:20]
    
    try:
        with open(HISTORY_FILE, 'w', encoding='utf-8') as f:
            json.dump(history, f, indent=4, ensure_ascii=False)
    except Exception as e:
        logger.error(f"Error guardando historial: {e}")

async def historial_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Muestra los últimos productos guardados"""
    history = load_history()
    
    if not history:
        await update.message.reply_text("📭 El historial está vacío.")
        return ConversationHandler.END
        
    keyboard = []
    for item in history[:10]: # Mostrar máximo 10 botones
        # Botón con Título corto y ASIN
        titulo_corto = item['titulo'][:25] + "..." if len(item['titulo']) > 25 else item['titulo']
        keyboard.append([InlineKeyboardButton(f"{titulo_corto} ({item['asin']})", callback_data=f"hist_{item['asin']}")])
        
    keyboard.append([InlineKeyboardButton("❌ Cancelar", callback_data="hist_cancelar")])
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await update.message.reply_text(
        "📜 **Historial de Productos**\n\n"
        "Selecciona un producto para volver a publicarlo sin buscar de nuevo:",
        reply_markup=reply_markup,
        parse_mode='Markdown'
    )
    return ESPERANDO_ASIN # Reusamos este estado para capturar el callback del historial

async def seleccionar_historial(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Carga un producto desde el historial"""
    query = update.callback_query
    await query.answer()
    
    if query.data == "hist_cancelar":
        await query.message.edit_text("❌ Historial cerrado.")
        return ConversationHandler.END
        
    asin_seleccionado = query.data.replace("hist_", "")
    history = load_history()
    
    # Buscar el producto en el historial
    datos = next((item for item in history if item['asin'] == asin_seleccionado), None)
    
    if not datos:
        await query.message.reply_text("❌ Error: Producto no encontrado en historial.")
        return ConversationHandler.END
        
    # Guardar en contexto de usuario y proceder como si se hubiera buscado
    user_data_storage[update.effective_user.id] = datos
    
    # Mostrar el producto y pedir review primero
    return await mostrar_producto_encontrado(update, context, datos, is_callback=True)

def obtener_datos_amazon(asin_raw):
    """Obtiene datos del producto de Amazon usando el ASIN con reintentos y soporte extendido"""
    
    # Limpiar y extraer ASIN real si viene una URL o texto sucio
    import re
    asin = asin_raw.strip()
    
    # Intentar buscar patrón de ASIN (10 caracteres alfanuméricos comenzando por B0)
    match = re.search(r'(B0[A-Z0-9]{8})', asin)
    if match:
        asin = match.group(1)
    
    # Intentar diferentes formatos de URL para evitar 404
    urls_to_try = [
        f"https://www.amazon.es/dp/{asin}",
        f"https://www.amazon.es/gp/product/{asin}"
    ]
    
    session = requests.Session()
    max_retries = 3
    
    for url in urls_to_try:
        for i in range(max_retries):
            try:
                # Timeout corto para no congelar
                response = session.get(url, headers=get_headers(), timeout=15)
                
                if "api-services-support@amazon.com" in response.text:
                    logger.warning(f"Captcha detectado en {url} intento {i+1}")
                    continue
                    
                if response.status_code == 200:
                    soup = BeautifulSoup(response.content, 'html.parser')
                    
                    # --- EXTRACCIÓN DEL TÍTULO ---
                    titulo = None
                    titulo_elem = soup.find('span', {'id': 'productTitle'}) or soup.find('h1', {'id': 'title'})
                    if not titulo_elem:
                        titulo_meta = soup.find('meta', {'name': 'title'})
                        if titulo_meta: titulo = titulo_meta.get('content')
                    
                    if titulo_elem: titulo = titulo_elem.get_text().strip()
                        
                    if not titulo: continue

                    # --- EXTRACCIÓN DE LA IMAGEN ---
                    imagen_url = None
                    img_elem = soup.find('img', {'id': 'landingImage'}) or \
                               soup.find('img', {'class': 'a-dynamic-image'}) or \
                               soup.find('img', {'data-a-image-name': 'landingImage'})
                               
                    if not img_elem:
                        wrapper = soup.find('div', {'id': 'imgTagWrapperId'})
                        if wrapper: img_elem = wrapper.find('img')

                    if img_elem:
                        imagen_url = img_elem.get('src') or img_elem.get('data-old-hires')

                    # --- DESCRIPCIÓN ---
                    descripcion = []
                    features = soup.find('div', {'id': 'feature-bullets'})
                    if features:
                        items = features.find_all('span', {'class': 'a-list-item'})
                        for item in items[:5]:
                            texto = item.get_text().strip()
                            if texto: descripcion.append(texto)
                    
                    # Devolvemos el objeto, recortando textos largos por seguridad inicial
                    return {
                        'titulo': titulo,
                        'imagen_url': imagen_url,
                        'descripcion': '\n'.join(descripcion),
                        'asin': asin
                    }
                    
            except Exception as e:
                logger.error(f"Error scraping {url}: {e}")
                import time
                time.sleep(1)
                
    return None

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Inicia la conversación"""
    await update.message.reply_text(
        "¡Hola! 👋\n\n"
        "Envíame el **ASIN** o enlace del producto.\n"
        "Ejemplo: B08N5WRWNW\n\n"
        "Usa /historial para ver productos guardados.\nUsa /reset si el bot se bloquea.",
        parse_mode='Markdown'
    )
    return ESPERANDO_ASIN

async def recibir_asin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Recibe el ASIN y obtiene datos"""
    asin = update.message.text.strip()
    
    msg = await update.message.reply_text("🔍 Buscando en Amazon... (hasta 15s)")
    
    datos = obtener_datos_amazon(asin)
    
    try: await msg.delete()
    except: pass

    if not datos:
        await update.message.reply_text("❌ No encontrado. Verifica el ASIN o intenta más tarde.\n\nSi usas un enlace, asegúrate de que contenga el ASIN.")
        return ConversationHandler.END
    
    # Guardar en historial
    save_history(datos)
    
    # Guardar en sesión temporal
    user_data_storage[update.effective_user.id] = datos
    
    return await mostrar_producto_encontrado(update, context, datos, is_callback=False)

async def mostrar_producto_encontrado(update, context, datos, is_callback=False):
    """Muestra el producto y los botones de REVIEW (Paso 1)"""
    # AHORA MOSTRAMOS PRIMERO LOS BOTONES DE REVIEW
    keyboard = [
        [InlineKeyboardButton("⭐️ Rating + 5 Estrellas", callback_data="review_rating")],
        [InlineKeyboardButton("🔥 Solo Compra", callback_data="review_compra")],
        [InlineKeyboardButton("📝 Solo Texto", callback_data="review_texto")],
        [InlineKeyboardButton("📷 Foto", callback_data="review_foto")],
        [InlineKeyboardButton("📹 Video", callback_data="review_video")],
        [InlineKeyboardButton("📷📹 Foto y Video", callback_data="review_fotovideo")],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    # --- CORTE DE SEGURIDAD PARA EVITAR ERROR DE TELEGRAM (Max 1024 chars) ---
    titulo_safe = datos['titulo']
    if len(titulo_safe) > 300:
        titulo_safe = titulo_safe[:300] + "..."
        
    desc_safe = datos['descripcion']
    if len(desc_safe) > 500:
        desc_safe = desc_safe[:500] + "..."
    
    # Mensaje inicial
    mensaje = f"**{titulo_safe}**\n\n{desc_safe}\n\n👇 **PASO 1: Selecciona el tipo de Review:**"
    
    target = update.callback_query.message if is_callback else update.message
    
    try:
        if is_callback:
            if datos['imagen_url']:
                await target.reply_photo(photo=datos['imagen_url'], caption=mensaje, parse_mode='Markdown', reply_markup=reply_markup)
            else:
                await target.reply_text(mensaje, parse_mode='Markdown', reply_markup=reply_markup)
        else:
            if datos['imagen_url']:
                await target.reply_photo(photo=datos['imagen_url'], caption=mensaje, parse_mode='Markdown', reply_markup=reply_markup)
            else:
                await target.reply_text(mensaje, parse_mode='Markdown', reply_markup=reply_markup)
    except Exception as e:
        logger.error(f"Error enviando vista previa: {e}")
        await target.reply_text(f"**{titulo_safe}**\n\n(Error cargando imagen)\n\n👇 **PASO 1: Selecciona el tipo de Review:**", parse_mode='Markdown', reply_markup=reply_markup)
            
    return SELECCIONANDO_REVIEW

async def seleccionar_review(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Guarda la review y muestra los botones de OFERTA (Paso 2)"""
    query = update.callback_query
    await query.answer()
    
    review_type = query.data.replace("review_", "")
    user_id = update.effective_user.id
    
    if user_id in user_data_storage:
        user_data_storage[user_id]['tipo_review'] = review_type
        
    # Ahora preguntamos la OFERTA
    keyboard = [
        [InlineKeyboardButton("💰 Reembolso completo", callback_data="oferta_completo")],
        [InlineKeyboardButton("💵 Reembolso parcial", callback_data="oferta_parcial")],
        [InlineKeyboardButton("💶 Euros (Personalizado)", callback_data="oferta_euros")],
        [InlineKeyboardButton("📋 Consultar condiciones", callback_data="oferta_consultar")],
    ]
    
    await query.message.reply_text("👇 **PASO 2: Selecciona el tipo de Reembolso/Oferta:**", reply_markup=InlineKeyboardMarkup(keyboard))
    return SELECCIONANDO_OFERTA

async def seleccionar_oferta(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Maneja la selección de la oferta y pasa a enlace"""
    query = update.callback_query
    await query.answer()
    
    tipo = query.data.replace("oferta_", "")
    user_id = update.effective_user.id
    
    if tipo == "euros":
        await query.message.reply_text("✍️ Escribe el texto de la oferta (ej: Menos 10€):")
        return ESPERANDO_TEXTO_EUROS
        
    if user_id in user_data_storage:
        user_data_storage[user_id]['tipo_oferta'] = tipo
        
    # Si no es euros, preguntamos directamente el enlace
    return await preguntar_tipo_enlace(update, context)

async def recibir_texto_euros(update: Update, context: ContextTypes.DEFAULT_TYPE):
    texto = update.message.text.strip()
    user_id = update.effective_user.id
    
    if user_id in user_data_storage:
        user_data_storage[user_id]['tipo_oferta'] = 'euros'
        user_data_storage[user_id]['texto_personalizado'] = texto
        
    return await preguntar_tipo_enlace(update, context)

async def preguntar_tipo_enlace(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Pregunta qué tipo de enlace generar"""
    target = update.message if update.message else update.callback_query.message
    
    keyboard = [
        [InlineKeyboardButton("🔗 Enlace Simple", callback_data="enlace_simple")],
        [InlineKeyboardButton("🔍 Enlace con Keywords", callback_data="enlace_keywords")],
    ]
    await target.reply_text("👇 **PASO 3: ¿Qué tipo de enlace?**", reply_markup=InlineKeyboardMarkup(keyboard))
    return SELECCIONANDO_TIPO_ENLACE

async def seleccionar_tipo_enlace(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    if query.data == "enlace_simple":
        user_id = update.effective_user.id
        asin = user_data_storage[user_id]['asin']
        enlace = f"https://www.amazon.es/dp/{asin}?linkCode=sl1&tag=martaadd-21"
        user_data_storage[user_id]['enlace'] = enlace
        
        # Enlace sin formato código
        await query.message.reply_text(f"🔗 **Enlace:**\n{enlace}", parse_mode='Markdown')
        return await preguntar_grupos(update, context)
        
    elif query.data == "enlace_keywords":
        await query.message.reply_text("🔍 Envía la palabra clave:")
        return ESPERANDO_KEYWORD

async def recibir_keyword(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyword = update.message.text.strip()
    user_id = update.effective_user.id
    
    enlace = f"https://www.amazon.es/s?k={urllib.parse.quote(keyword)}&linkCode=sl1&tag=martaadd-21"
    if user_id in user_data_storage:
        user_data_storage[user_id]['enlace'] = enlace
        
    await update.message.reply_text(f"🔗 **Enlace Keyword:**\n{enlace}", parse_mode='Markdown')
    return await preguntar_grupos(update, context)

async def preguntar_grupos(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not GRUPOS_CONFIG:
        await update.effective_message.reply_text("⚠️ No hay grupos configurados.")
        return ConversationHandler.END
    
    keyboard = []
    for gid, name in GRUPOS_CONFIG.items():
        keyboard.append([InlineKeyboardButton(f"📢 {name}", callback_data=f"grupo_{gid}")])
    
    keyboard.append([InlineKeyboardButton("✅ Publicar en TODOS", callback_data="grupo_todos")])
    keyboard.append([InlineKeyboardButton("❌ Cancelar", callback_data="grupo_cancelar")])
    
    await update.effective_message.reply_text("📢 **¿Dónde publicar?**", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')
    return SELECCIONANDO_GRUPOS

async def publicar_en_grupos(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    if query.data == "grupo_cancelar":
        await query.message.reply_text("❌ Cancelado.")
        return ConversationHandler.END

    user_id = update.effective_user.id
    datos = user_data_storage.get(user_id)
    if not datos:
        await query.message.reply_text("❌ Error de sesión. Usa /start")
        return ConversationHandler.END
        
    targets = list(GRUPOS_CONFIG.keys()) if query.data == "grupo_todos" else [int(query.data.replace("grupo_", ""))]
    
    # --- CONSTRUCCIÓN DEL MENSAJE FINAL ---
    tipo_oferta = datos.get('tipo_oferta')
    if tipo_oferta == 'euros':
        texto_oferta = f"💶 {datos.get('texto_personalizado', 'Oferta Especial')}"
    else:
        oferta_map = {
            "completo": "💰 Reembolso completo",
            "parcial": "💵 Reembolso parcial",
            "consultar": "📋 Consultar condiciones"
        }
        texto_oferta = oferta_map.get(tipo_oferta, "📋 Consultar condiciones")
    
    review_map = {
        "rating": "⭐️ Rating 5 Estrellas",
        "compra": "🔥 Solo Compra",
        "texto": "📝 Solo Texto",
        "foto": "📷 Review con Foto",
        "video": "📹 Review con Video",
        "fotovideo": "📷📹 Review con Foto y Video"
    }
    texto_review = review_map.get(datos.get('tipo_review'), "")
    
    # Recortar título para seguridad en publicación
    titulo_pub = datos['titulo']
    if len(titulo_pub) > 800: titulo_pub = titulo_pub[:800] + "..."

    # FORMATO VISUAL "TARJETA PROFESIONAL"
    # Diseñado para diferenciar y atraer
    mensaje_publicacion = (
        f"🔹 *{titulo_pub}*\n\n"
        f"{texto_review}\n"
        f"{texto_oferta}\n\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"📩 *Contacto:* @R0cksta"
    )
    
    count = 0
    for chat_id in targets:
        try:
            if datos['imagen_url']:
                await context.bot.send_photo(chat_id=chat_id, photo=datos['imagen_url'], caption=mensaje_publicacion, parse_mode='Markdown')
            else:
                await context.bot.send_message(chat_id=chat_id, text=mensaje_publicacion, parse_mode='Markdown')
            count += 1
        except Exception as e:
            logger.error(f"Error en {chat_id}: {e}")
            
    await query.message.reply_text(f"✅ Publicado en {count} grupos.")
    
    # --- MENÚ POST-PUBLICACIÓN (BUCLE) ---
    keyboard = [
        [InlineKeyboardButton("🔙 Publicar en otro grupo", callback_data="post_volver")],
        [InlineKeyboardButton("🏁 Terminar", callback_data="post_terminar")]
    ]
    await query.message.reply_text("¿Qué quieres hacer ahora?", reply_markup=InlineKeyboardMarkup(keyboard))
    return POST_PUBLICACION

async def post_publicacion_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Maneja el bucle después de publicar"""
    query = update.callback_query
    await query.answer()
    
    if query.data == "post_volver":
        return await preguntar_grupos(update, context)
    else:
        user_id = update.effective_user.id
        if user_id in user_data_storage: del user_data_storage[user_id]
        await query.message.edit_text("👋 ¡Hasta luego!")
        return ConversationHandler.END

async def cancelar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("❌ Cancelado.")
    return ConversationHandler.END

async def reset_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id in user_data_storage: del user_data_storage[user_id]
    await update.message.reply_text("🔄 Bot reiniciado.")
    return ConversationHandler.END

async def obtener_id_grupo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    await update.message.reply_text(f"ID: `{chat.id}`\nNombre: {chat.title}", parse_mode='Markdown')

def main():
    application = Application.builder().token(TOKEN).build()
    
    conv_handler = ConversationHandler(
        entry_points=[
            CommandHandler('start', start),
            CommandHandler('historial', historial_command)
        ],
        states={
            ESPERANDO_ASIN: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, recibir_asin),
                
            # Manejador para selección de historial dentro de ESPERANDO_ASIN
            CallbackQueryHandler(seleccionar_historial, pattern='^hist_'),
            CommandHandler('historial', historial_command),
  
            ],
            # SECCIÓN ORDENADA: REVIEW -> OFERTA -> EUROS (si aplica) -> ENLACE
            SELECCIONANDO_REVIEW: [CallbackQueryHandler(seleccionar_review, pattern='^review_')],
            SELECCIONANDO_OFERTA: [CallbackQueryHandler(seleccionar_oferta, pattern='^oferta_')],
            ESPERANDO_TEXTO_EUROS: [MessageHandler(filters.TEXT & ~filters.COMMAND, recibir_texto_euros)],
            SELECCIONANDO_TIPO_ENLACE: [CallbackQueryHandler(seleccionar_tipo_enlace, pattern='^enlace_')],
            ESPERANDO_KEYWORD: [MessageHandler(filters.TEXT & ~filters.COMMAND, recibir_keyword)],
            SELECCIONANDO_GRUPOS: [CallbackQueryHandler(publicar_en_grupos, pattern='^grupo_')],
            POST_PUBLICACION: [CallbackQueryHandler(post_publicacion_handler, pattern='^post_')]
        },
        fallbacks=[CommandHandler('cancelar', cancelar), CommandHandler('reset', reset_command)],
    )
    
    application.add_handler(conv_handler)
    application.add_handler(CommandHandler('grupoid', obtener_id_grupo))
    
    
    # Iniciar servidor web dummy en segundo plano para Render
    start_server_thread()

    logger.info("🚀 Bot iniciado correctamente!")
    application.run_polling()

if __name__ == '__main__':
    main()