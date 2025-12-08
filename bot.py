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
from PIL import Image
from io import BytesIO

# Logging
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

# Token
TOKEN = "5542545245:AAETXpqyI9htrp760FqZ1TmI7N0A4zKhY1I"

# Public Groups
GRUPOS_CONFIG = {
    -1001593262082: "Rockstar Store",
    -1001521082473: "Rockstar Spain",
    -1001461104385: "Spain Game"
}

# Archive Channels
ARCHIVO_PRODUCTOS_ID = -1001656228551
ARCHIVO_ENLACES_ID = -1001915798478

# States
ESPERANDO_ASIN, SELECCIONANDO_REVIEW, SELECCIONANDO_OFERTA, ESPERANDO_TEXTO_EUROS, SELECCIONANDO_TIPO_ENLACE, ESPERANDO_KEYWORD, SELECCIONANDO_GRUPOS, POST_PUBLICACION = range(8)

# User Data
user_data_storage = {}

# User Agents
USER_AGENTS = [
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.2 Safari/605.1.15'
]

def get_headers():
    return {
        'User-Agent': random.choice(USER_AGENTS),
        'Accept-Language': 'es-ES,es;q=0.9',
        'Referer': 'https://www.amazon.es/'
    }

# Dummy Server
def run_dummy_server():
    PORT = int(os.environ.get("PORT", 8080))
    Handler = http.server.SimpleHTTPRequestHandler
    class HealthCheckHandler(Handler):
        def do_GET(self):
            self.send_response(200)
            self.wfile.write(b"Bot is alive!")
    with socketserver.TCPServer(("", PORT), HealthCheckHandler) as httpd:
        httpd.serve_forever()

def start_server_thread():
    threading.Thread(target=run_dummy_server, daemon=True).start()

# Image Processing
def agregar_marca_agua(url_imagen_producto):
    try:
        response = requests.get(url_imagen_producto, headers=get_headers(), timeout=10)
        img_producto = Image.open(BytesIO(response.content)).convert("RGBA")
        
        # LOGO ROCKSTAR - CAMBIA LA URL DE ABAJO POR LA TUYA SI TIENES UNA (Debe ser link directo a .png/.jpg)
        url_logo = "https://upload.wikimedia.org/wikipedia/commons/thumb/e/ee/Rockstar_Toronto_Logo.svg/512px-Rockstar_Toronto_Logo.svg.png"
        
        response_logo = requests.get(url_logo, headers=get_headers(), timeout=10)
        img_logo = Image.open(BytesIO(response_logo.content)).convert("RGBA")
        
        # Redimensionado al 12% (Pequeño y elegante)
        ancho_base = int(img_producto.width * 0.12)
        w_percent = (ancho_base / float(img_logo.size[0]))
        h_size = int((float(img_logo.size[1]) * float(w_percent)))
        img_logo = img_logo.resize((ancho_base, h_size), Image.Resampling.LANCZOS)
        
        position = (20, 20)
        temp_img = Image.new('RGBA', img_producto.size, (0,0,0,0))
        temp_img.paste(img_producto, (0,0))
        temp_img.paste(img_logo, position, mask=img_logo)
        
        output = BytesIO()
        bg = Image.new("RGB", temp_img.size, (255, 255, 255))
        bg.paste(temp_img, mask=temp_img.split()[3])
        bg.save(output, format="JPEG", quality=95)
        output.seek(0)
        return output
    except Exception as e:
        logger.error(f"Error imagen: {e}")
        return None

# Amazon Scraper
def obtener_datos_amazon(asin_raw):
    import re
    asin = asin_raw.strip()
    match = re.search(r'(B0[A-Z0-9]{8})', asin)
    if match: asin = match.group(1)
    
    urls = [f"https://www.amazon.es/dp/{asin}", f"https://www.amazon.es/gp/product/{asin}"]
    session = requests.Session()
    
    for url in urls:
        for _ in range(2):
            try:
                r = session.get(url, headers=get_headers(), timeout=10)
                if r.status_code == 200:
                    soup = BeautifulSoup(r.content, 'html.parser')
                    titulo = soup.find('span', {'id': 'productTitle'}) or soup.find('h1', {'id': 'title'})
                    if not titulo: continue
                    titulo = titulo.get_text().strip()
                    
                    img = soup.find('img', {'id': 'landingImage'}) or soup.find('img', {'class': 'a-dynamic-image'})
                    img_url = img.get('src') if img else None
                    
                    return {'titulo': titulo, 'imagen_url': img_url, 'asin': asin}
            except: pass
    return None

# Handlers
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("👋 **Rockstar Bot**\nEnvíame ASIN/Link.", parse_mode='Markdown')
    return ESPERANDO_ASIN

async def recibir_asin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    asin = update.message.text
    msg = await update.message.reply_text("🔍 Buscando...")
    datos = obtener_datos_amazon(asin)
    try: await msg.delete()
    except: pass
    
    if not datos:
        await update.message.reply_text("❌ No encontrado.")
        return ConversationHandler.END
        
    user_data_storage[update.effective_user.id] = datos
    
    # ASK REVIEW FIRST
    keyboard = [
        [InlineKeyboardButton("⭐️ Rating +5 Estrellas", callback_data="rev_rating")],
        [InlineKeyboardButton("🔥 Solo Compra", callback_data="rev_compra")],
        [InlineKeyboardButton("📝 Solo Texto", callback_data="rev_texto")],
        [InlineKeyboardButton("📷 Foto", callback_data="rev_foto")],
        [InlineKeyboardButton("📹 Video", callback_data="rev_video")],
        [InlineKeyboardButton("📷📹 Foto y Video", callback_data="rev_ambos")]
    ]
    await update.message.reply_text("1️⃣ **Selecciona Tipo de Review:**", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')
    return SELECCIONANDO_REVIEW

async def seleccionar_review(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    mapa_reviews = {
        "rev_rating": "⭐️ Rating +5 Estrellas",
        "rev_compra": "🔥 Solo Compra",
        "rev_texto": "📝 Solo Texto",
        "rev_foto": "📷 Foto",
        "rev_video": "📹 Video",
        "rev_ambos": "📷📹 Foto y Video"
    }
    
    uid = update.effective_user.id
    if uid in user_data_storage:
        user_data_storage[uid]['review'] = mapa_reviews.get(query.data, "Review")
        
    # ASK OFFER SECOND
    keyboard = [
        [InlineKeyboardButton("Reembolso completo", callback_data="off_completo")],
        [InlineKeyboardButton("Reembolso parcial", callback_data="off_parcial")],
        [InlineKeyboardButton("Consultar condiciones", callback_data="off_consultar")],
        [InlineKeyboardButton("💶 Euros (Escribir)", callback_data="off_euros")]
    ]
    await query.message.edit_text(f"✅ Review: {user_data_storage[uid]['review']}\n\n2️⃣ **Selecciona Oferta:**", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')
    return SELECCIONANDO_OFERTA

async def seleccionar_oferta(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    uid = update.effective_user.id
    tipo = query.data
    
    if tipo == "off_euros":
        await query.message.reply_text("✍️ Escribe el texto de la oferta (ej: 'Menos 10€'):")
        return ESPERANDO_TEXTO_EUROS
        
    mapa_ofertas = {
        "off_completo": "Reembolso completo",
        "off_parcial": "Reembolso parcial",
        "off_consultar": "Consultar condiciones"
    }
    if uid in user_data_storage:
        user_data_storage[uid]['oferta'] = mapa_ofertas.get(tipo, "Consultar")
        
    return await preguntar_tipo_enlace(update, context)

async def recibir_texto_euros(update: Update, context: ContextTypes.DEFAULT_TYPE):
    texto = update.message.text
    uid = update.effective_user.id
    if uid in user_data_storage:
        user_data_storage[uid]['oferta'] = texto
    return await preguntar_tipo_enlace(update, context)

async def preguntar_tipo_enlace(update, context):
    keyboard = [
        [InlineKeyboardButton("🔗 Simple", callback_data="link_simple")],
        [InlineKeyboardButton("🔍 Keywords", callback_data="link_key")]
    ]
    if update.message:
        await update.message.reply_text("3️⃣ **Tipo de Enlace:**", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')
    else:
        await update.callback_query.message.edit_text("3️⃣ **Tipo de Enlace:**", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')
    return SELECCIONANDO_TIPO_ENLACE

async def seleccionar_tipo_enlace(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    if query.data == "link_key":
        await query.message.reply_text("🔍 Escribe las keywords:")
        return ESPERANDO_KEYWORD
        
    uid = update.effective_user.id
    asin = user_data_storage[uid]['asin']
    link = f"https://www.amazon.es/dp/{asin}?linkCode=sl1&tag=martaadd-21"
    
    return await finalizar_enlace(update, context, link)

async def recibir_keyword(update: Update, context: ContextTypes.DEFAULT_TYPE):
    kw = urllib.parse.quote(update.message.text)
    link = f"https://www.amazon.es/s?k={kw}&linkCode=sl1&tag=martaadd-21"
    return await finalizar_enlace(update, context, link)

async def finalizar_enlace(update, context, link):
    uid = update.effective_user.id
    user_data_storage[uid]['enlace'] = link
    datos = user_data_storage[uid]
    
    # 1. SEND LINK TO USER (AND ARCHIVE)
    msg_user = f"🔗 **Enlace Generado:**\n{link}"
    if update.callback_query:
        await update.callback_query.message.reply_text(msg_user)
    else:
        await update.message.reply_text(msg_user)
        
    try:
        msg_archive = f"📦 **{datos['titulo']}**\n🔗 {link}"
        await context.bot.send_message(chat_id=ARCHIVO_ENLACES_ID, text=msg_archive)
    except Exception as e:
        logger.error(f"Error archivo enlaces: {e}")
        
    # 2. GENERATE PREVIEW (ALWAYS)
    status_msg = await context.bot.send_message(chat_id=update.effective_chat.id, text="🎨 Generando Vista Previa con Logo...")
    
    titulo = datos['titulo']
    if len(titulo) > 800: titulo = titulo[:800] + "..."
    
    msg_card = (
        f"**{titulo}**\n"
        "__________________\n\n"
        f"**{datos['review']}**\n"
        f"**{datos['oferta']}**\n"
        "__________________\n\n"
        "**Contacto:** @R0cksta"
    )
    
    img_data = None
    if datos['imagen_url']:
        img_data = agregar_marca_agua(datos['imagen_url'])
        
    # Send Preview
    try:
        await status_msg.delete()
        if img_data:
            img_data.seek(0)
            await context.bot.send_photo(chat_id=update.effective_chat.id, photo=img_data, caption=msg_card, parse_mode='Markdown')
        elif datos['imagen_url']:
             await context.bot.send_photo(chat_id=update.effective_chat.id, photo=datos['imagen_url'], caption=msg_card, parse_mode='Markdown')
        else:
             await context.bot.send_message(chat_id=update.effective_chat.id, text=msg_card, parse_mode='Markdown')
    except Exception as e:
        logger.error(f"Error enviando preview: {e}")
        await context.bot.send_message(chat_id=update.effective_chat.id, text="⚠️ Error generando vista previa de imagen, pero la publicación funciona.")

    # 3. ASK GROUPS
    return await preguntar_grupos(update, context)

async def preguntar_grupos(update, context):
    keyboard = []
    for gid, name in GRUPOS_CONFIG.items():
        keyboard.append([InlineKeyboardButton(name, callback_data=f"g_{gid}")])
    keyboard.append([InlineKeyboardButton("✅ TODOS", callback_data="g_todos")])
    keyboard.append([InlineKeyboardButton("❌ Cancelar", callback_data="cancel")])
    
    # We send a new message because the previous one was the photo preview
    await context.bot.send_message(chat_id=update.effective_chat.id, text="📢 **¿Dónde publicar?**", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')
    return SELECCIONANDO_GRUPOS

async def publicar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    if query.data == "cancel":
        await query.message.edit_text("❌ Cancelado")
        return ConversationHandler.END
        
    uid = update.effective_user.id
    datos = user_data_storage.get(uid)
    
    # TARGETS
    if query.data == "g_todos":
        targets = list(GRUPOS_CONFIG.keys())
    else:
        targets = [int(query.data.replace("g_", ""))]
        
    # ADD ARCHIVE PRODUCT CHANNEL ALWAYS
    targets.append(ARCHIVO_PRODUCTOS_ID)
    
    # MESSAGE FORMAT (CARD)
    titulo = datos['titulo']
    if len(titulo) > 800: titulo = titulo[:800] + "..."
    
    msg = (
        f"**{titulo}**\n"
        "__________________\n\n"
        f"**{datos['review']}**\n"
        f"**{datos['oferta']}**\n"
        "__________________\n\n"
        "**Contacto:** @R0cksta"
    )
    
    # IMAGE PROCESSING
    await query.message.edit_text("🚀 Publicando...")
    
    img_data = None
    if datos['imagen_url']:
        img_data = agregar_marca_agua(datos['imagen_url'])
        
    count = 0
    for chat_id in targets:
        try:
            if img_data:
                img_data.seek(0) # Reset pointer
                await context.bot.send_photo(chat_id=chat_id, photo=img_data, caption=msg, parse_mode='Markdown')
            elif datos['imagen_url']:
                await context.bot.send_photo(chat_id=chat_id, photo=datos['imagen_url'], caption=msg, parse_mode='Markdown')
            else:
                await context.bot.send_message(chat_id=chat_id, text=msg, parse_mode='Markdown')
            
            if chat_id != ARCHIVO_PRODUCTOS_ID: count += 1
        except Exception as e:
            logger.error(f"Error enviando a {chat_id}: {e}")

    await query.message.reply_text(f"✅ Publicado en {count} grupos públicos + Archivo.")
    return ConversationHandler.END

def main():
    application = Application.builder().token(TOKEN).build()
    
    conv = ConversationHandler(
        entry_points=[CommandHandler('start', start)],
        states={
            ESPERANDO_ASIN: [MessageHandler(filters.TEXT & ~filters.COMMAND, recibir_asin)],
            SELECCIONANDO_REVIEW: [CallbackQueryHandler(seleccionar_review)],
            SELECCIONANDO_OFERTA: [CallbackQueryHandler(seleccionar_oferta)],
            ESPERANDO_TEXTO_EUROS: [MessageHandler(filters.TEXT & ~filters.COMMAND, recibir_texto_euros)],
            SELECCIONANDO_TIPO_ENLACE: [CallbackQueryHandler(seleccionar_tipo_enlace)],
            ESPERANDO_KEYWORD: [MessageHandler(filters.TEXT & ~filters.COMMAND, recibir_keyword)],
            SELECCIONANDO_GRUPOS: [CallbackQueryHandler(publicar)]
        },
        fallbacks=[CommandHandler('cancel', start)]
    )
    
    application.add_handler(conv)
    start_server_thread()
    application.run_polling()

if __name__ == '__main__':
    main()