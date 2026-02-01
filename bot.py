import os
import sys
import re
import requests
import json
import uuid
import math
import threading
import time
import urllib.parse
from flask import Flask, render_template_string, request, redirect, url_for, Response, jsonify
from pymongo import MongoClient
from bson.objectid import ObjectId
from dotenv import load_dotenv
from datetime import datetime

# --- কনফিগারেশন লোড ---
load_dotenv()

app = Flask(__name__)
app.secret_key = os.urandom(24)

# --- ভেরিয়েবলসমূহ (আপনার .env ফাইল থেকে আসবে) ---
MONGO_URI = os.getenv("MONGO_URI")
TMDB_API_KEY = os.getenv("TMDB_API_KEY")
BOT_TOKEN = os.getenv("BOT_TOKEN")
BOT_USERNAME = os.getenv("BOT_USERNAME")
PUBLIC_CHANNEL_ID = os.getenv("PUBLIC_CHANNEL_ID")
SOURCE_CHANNEL_ID = os.getenv("SOURCE_CHANNEL_ID")
WEBSITE_URL = os.getenv("WEBSITE_URL")
TELEGRAM_API_URL = f"https://api.telegram.org/bot{BOT_TOKEN}"

# রিকোয়েস্ট চ্যানেলের লিংক (এখানে আপনার লিংক দিন)
REQUEST_CHANNEL = "https://t.me/YOUR_REQUEST_CHANNEL" 

# এডমিন ইউজারনেম ও পাসওয়ার্ড
ADMIN_USER = os.getenv("ADMIN_USERNAME", "admin")
ADMIN_PASS = os.getenv("ADMIN_PASSWORD", "admin")

# অটো ডিলিট ও নোটিফিকেশন সেটিংস
DELETE_TIMEOUT = 600 
NOTIFICATION_COOLDOWN = 1800 

# --- ডেটাবেস কানেকশন ---
try:
    client = MongoClient(MONGO_URI)
    db = client["moviezone_db"]
    movies = db["movies"]
    settings = db["settings"]
    categories = db["categories"] 
    print("✅ MongoDB Connected Successfully!")
except Exception as e:
    print(f"❌ MongoDB Connection Error: {e}")
    sys.exit(1)

# === Helper Functions ===

def clean_filename(filename):
    name = os.path.splitext(filename)[0]
    name = re.sub(r'[._\-\+\[\]\(\)]', ' ', name)
    stop_pattern = r'(\b(19|20)\d{2}\b|\bS\d+|\bSeason|\bEp?\s*\d+|\b480p|\b720p|\b1080p|\b2160p|\bHD|\bWeb-?dl|\bBluray|\bDual|\bHindi|\bBangla)'
    match = re.search(stop_pattern, name, re.IGNORECASE)
    if match: name = name[:match.start()]
    return re.sub(r'\s+', ' ', name).strip()

def get_file_quality(filename):
    filename = filename.lower()
    if "4k" in filename or "2160p" in filename: return "4K UHD"
    if "1080p" in filename: return "1080p FHD"
    if "720p" in filename: return "720p HD"
    if "480p" in filename: return "480p SD"
    return "HD"

def detect_language(text):
    text = text.lower()
    if re.search(r'\b(multi|multi audio)\b', text): return "Multi Audio"
    if re.search(r'\b(dual|dual audio)\b', text): return "Dual Audio"
    if "bangla" in text or "bengali" in text: return "Bangla"
    if "hindi" in text: return "Hindi"
    if "english" in text: return "English"
    if "japanese" in text: return "Japanese"
    return "Japanese/English"

def get_episode_label(filename):
    season = ""
    match_s = re.search(r'\b(S|Season)\s*(\d+)', filename, re.IGNORECASE)
    if match_s: season = f"S{int(match_s.group(2)):02d}"

    match_ep = re.search(r'\b(Episode|Ep|E)\s*(\d+)\b', filename, re.IGNORECASE)
    if match_ep:
        return f"{season} E{int(match_ep.group(2)):02d}".strip()
    return season if season else "Full Movie"

def extract_youtube_id(url):
    if not url: return None
    regex = r'(?:youtube\.com\/(?:[^\/]+\/.+\/|(?:v|e(?:mbed)?)\/|.*[?&]v=)|youtu\.be\/)([^"&?\/\s]{11})'
    match = re.search(regex, url)
    return match.group(1) if match else None

def delete_message_later(chat_id, message_id, delay):
    time.sleep(delay)
    try: requests.post(f"{TELEGRAM_API_URL}/deleteMessage", json={"chat_id": chat_id, "message_id": message_id})
    except: pass

def check_auth():
    auth = request.authorization
    if not auth or not (auth.username == ADMIN_USER and auth.password == ADMIN_PASS):
        return False
    return True

# TMDB থেকে ডিটেইলস আনার ফাংশন (বট এবং এডমিন উভয়ের জন্য)
def get_tmdb_details(title, content_type="movie", year=None):
    if not TMDB_API_KEY: return {"title": title}
    tmdb_type = "tv" if content_type == "series" else "movie"
    try:
        query_str = requests.utils.quote(title)
        search_url = f"https://api.themoviedb.org/3/search/{tmdb_type}?api_key={TMDB_API_KEY}&query={query_str}"
        if year and tmdb_type == "movie": search_url += f"&year={year}"

        data = requests.get(search_url, timeout=5).json()
        if data.get("results"):
            res = data["results"][0]
            m_id = res.get("id")
            details_url = f"https://api.themoviedb.org/3/{tmdb_type}/{m_id}?api_key={TMDB_API_KEY}&append_to_response=credits,videos"
            extra = requests.get(details_url, timeout=5).json()

            trailer_key = None
            if extra.get('videos', {}).get('results'):
                for vid in extra['videos']['results']:
                    if vid['type'] == 'Trailer' and vid['site'] == 'YouTube':
                        trailer_key = vid['key']; break
            
            genres = [g['name'] for g in extra.get('genres', [])]
            poster = f"https://image.tmdb.org/t/p/w500{res['poster_path']}" if res.get('poster_path') else None
            backdrop = f"https://image.tmdb.org/t/p/original{res['backdrop_path']}" if res.get('backdrop_path') else None

            return {
                "tmdb_id": res.get("id"),
                "title": res.get("name") if tmdb_type == "tv" else res.get("title"),
                "overview": res.get("overview"),
                "poster": poster,
                "backdrop": backdrop,
                "release_date": res.get("first_air_date") if tmdb_type == "tv" else res.get("release_date"),
                "vote_average": res.get("vote_average"),
                "genres": genres,        
                "trailer": trailer_key,  
                "adult": res.get("adult", False)
            }
    except: pass
    return {"title": title}

@app.context_processor
def inject_globals():
    return dict(ad_settings=settings.find_one() or {}, BOT_USERNAME=BOT_USERNAME, site_name="AnimeNexus", request_channel=REQUEST_CHANNEL)

# === TELEGRAM WEBHOOK (Auto Upload) ===
@app.route(f'/webhook/{BOT_TOKEN}', methods=['POST'])
def telegram_webhook():
    update = request.get_json()
    if not update: return jsonify({'status': 'ignored'})

    if 'channel_post' in update:
        msg = update['channel_post']
        chat_id = str(msg.get('chat', {}).get('id'))
        
        if SOURCE_CHANNEL_ID and chat_id != str(SOURCE_CHANNEL_ID): return jsonify({'status': 'wrong_channel'})

        file_id = None
        file_name = "Unknown"
        file_type = "document"

        if 'video' in msg:
            video = msg['video']
            file_id = video['file_id']
            file_name = video.get('file_name', msg.get('caption', 'Unknown Video'))
            file_type = "video"
        elif 'document' in msg:
            doc = msg['document']
            file_id = doc['file_id']
            file_name = doc.get('file_name', 'Unknown Document')
            file_type = "document"

        if not file_id: return jsonify({'status': 'no_file'})

        raw_caption = msg.get('caption')
        raw_input = raw_caption if raw_caption else file_name
        search_title = clean_filename(raw_input) 
        
        content_type = "movie"
        if re.search(r'(S\d+|Season|Episode|Ep\s*\d+)', file_name, re.IGNORECASE) or re.search(r'(S\d+|Season)', str(raw_caption), re.IGNORECASE):
            content_type = "series"

        # প্রাথমিক ডাটা (পরে এডমিন প্যানেল থেকে এডিট করা যাবে)
        tmdb_data = get_tmdb_details(search_title, content_type)
        final_title = tmdb_data.get('title', search_title)
        
        unique_code = str(uuid.uuid4())[:8]
        current_time = datetime.utcnow()

        file_obj = {
            "file_id": file_id,
            "unique_code": unique_code,
            "filename": file_name,
            "quality": get_file_quality(file_name),
            "episode_label": get_episode_label(file_name),
            "size": f"{(msg.get('document', {}).get('file_size', 0) / (1024*1024)):.2f} MB",
            "file_type": file_type,
            "added_at": current_time
        }

        existing_movie = movies.find_one({"title": final_title})
        movie_id = None

        if existing_movie:
            movies.update_one({"_id": existing_movie['_id']}, {"$push": {"files": file_obj}, "$set": {"updated_at": current_time}})
            movie_id = existing_movie['_id']
        else:
            new_movie = {
                "title": final_title,
                "overview": tmdb_data.get('overview'),
                "poster": tmdb_data.get('poster'),
                "backdrop": tmdb_data.get('backdrop'),
                "release_date": tmdb_data.get('release_date'),
                "vote_average": tmdb_data.get('vote_average'),
                "genres": tmdb_data.get('genres'),
                "trailer": tmdb_data.get('trailer'),
                "language": detect_language(raw_input),
                "type": content_type,
                "category": "Uncategorized",
                "is_adult": tmdb_data.get('adult', False),
                "files": [file_obj],
                "created_at": current_time,
                "updated_at": current_time
            }
            res = movies.insert_one(new_movie)
            movie_id = res.inserted_id

        # Update Telegram Post with Link
        if movie_id and WEBSITE_URL:
            direct_link = f"{WEBSITE_URL.rstrip('/')}/movie/{str(movie_id)}"
            try:
                requests.post(f"{TELEGRAM_API_URL}/editMessageReplyMarkup", json={
                    'chat_id': chat_id,
                    'message_id': msg['message_id'],
                    'reply_markup': json.dumps({"inline_keyboard": [[{"text": "▶️ Check on Website", "url": direct_link}]]})
                })
            except: pass

        return jsonify({'status': 'success'})

    # Bot Reply Logic
    elif 'message' in update:
        msg = update['message']
        chat_id = msg.get('chat', {}).get('id')
        text = msg.get('text', '')

        if text.startswith('/start'):
            parts = text.split()
            if len(parts) > 1:
                code = parts[1]
                movie = movies.find_one({"files.unique_code": code})
                if movie:
                    target_file = next((f for f in movie['files'] if f['unique_code'] == code), None)
                    if target_file:
                        caption = f"🎬 *{final_title if 'final_title' in locals() else movie['title']}*\n📌 {target_file['episode_label']}\n⚠️ *Link expires in 10 mins!*"
                        payload = {'chat_id': chat_id, 'caption': caption, 'parse_mode': 'Markdown'}
                        method = 'sendVideo' if target_file['file_type'] == 'video' else 'sendDocument'
                        
                        if target_file['file_type'] == 'video': payload['video'] = target_file['file_id']
                        else: payload['document'] = target_file['file_id']
                        
                        try:
                            resp = requests.post(f"{TELEGRAM_API_URL}/{method}", json=payload).json()
                            if resp.get('ok'):
                                threading.Thread(target=delete_message_later, args=(chat_id, resp['result']['message_id'], DELETE_TIMEOUT)).start()
                        except: pass
                    else:
                        requests.post(f"{TELEGRAM_API_URL}/sendMessage", json={'chat_id': chat_id, 'text': "❌ File expired."})
            else:
                requests.post(f"{TELEGRAM_API_URL}/sendMessage", json={'chat_id': chat_id, 'text': "👋 Welcome to Anime Nexus!"})

    return jsonify({'status': 'ok'})

# ================================
#        MODERN UI TEMPLATES (Anime Nexus)
# ================================

index_template = """
<!DOCTYPE html>
<html lang="en" class="dark">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{{ site_name }} - Anime Stream</title>
    <script src="https://cdn.tailwindcss.com"></script>
    <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.0/css/all.min.css">
    <link href="https://fonts.googleapis.com/css2?family=Outfit:wght@300;400;600;700&display=swap" rel="stylesheet">
    <link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/swiper@11/swiper-bundle.min.css" />
    <script>
        tailwind.config = {
            theme: {
                extend: { colors: { primary: '#8b5cf6', dark: '#09090b', card: '#18181b' }, fontFamily: { sans: ['Outfit', 'sans-serif'] } }
            }
        }
    </script>
    <style>
        body { background-color: #09090b; color: #fff; }
        .glass { background: rgba(24, 24, 27, 0.7); backdrop-filter: blur(10px); }
        .swiper-slide { height: 350px; border-radius: 16px; overflow: hidden; position: relative; }
        @media(min-width: 768px) { .swiper-slide { height: 450px; } }
    </style>
</head>
<body class="antialiased">
<nav class="fixed w-full z-50 glass border-b border-white/5 transition-all duration-300">
    <div class="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8">
        <div class="flex items-center justify-between h-16">
            <a href="/" class="text-2xl font-bold bg-gradient-to-r from-primary to-purple-400 bg-clip-text text-transparent uppercase">{{ site_name }}</a>
            <div class="flex items-center gap-4">
                <a href="https://t.me/{{ BOT_USERNAME }}" class="text-gray-300 hover:text-primary"><i class="fab fa-telegram text-xl"></i></a>
                <button onclick="document.getElementById('searchModal').classList.remove('hidden')" class="p-2 hover:text-white"><i class="fas fa-search"></i></button>
            </div>
        </div>
    </div>
</nav>

<div id="searchModal" class="fixed inset-0 z-[60] bg-black/90 hidden flex items-center justify-center p-4">
    <div class="w-full max-w-2xl relative">
        <button onclick="document.getElementById('searchModal').classList.add('hidden')" class="absolute -top-10 right-0 text-white text-xl"><i class="fas fa-times"></i></button>
        <form action="/" method="GET">
            <input type="text" name="q" placeholder="Search anime..." class="w-full bg-zinc-800 text-white text-xl px-6 py-4 rounded-full border border-zinc-700 focus:border-primary focus:outline-none placeholder-gray-500 shadow-2xl shadow-primary/20" autoFocus>
        </form>
    </div>
</div>

<div class="pt-20 pb-10 max-w-7xl mx-auto px-4 sm:px-6 lg:px-8">
    {% if slider_movies %}
    <div class="swiper mySwiper mb-10 shadow-2xl shadow-purple-900/10 rounded-2xl">
        <div class="swiper-wrapper">
            {% for slide in slider_movies %}
            <div class="swiper-slide group">
                <a href="{{ url_for('movie_detail', movie_id=slide._id) }}" class="block w-full h-full relative">
                    <img src="{{ slide.backdrop or slide.poster }}" class="w-full h-full object-cover transform group-hover:scale-105 transition duration-700">
                    <div class="absolute inset-0 bg-gradient-to-t from-black via-black/40 to-transparent"></div>
                    <div class="absolute bottom-0 left-0 p-6 md:p-10 w-full">
                        <span class="bg-primary text-xs font-bold px-3 py-1 rounded-full uppercase mb-3 inline-block">Trending</span>
                        <h2 class="text-3xl md:text-5xl font-bold text-white mb-2">{{ slide.title }}</h2>
                        <div class="flex items-center gap-3 text-sm text-gray-300">
                            <span><i class="fas fa-star text-yellow-500"></i> {{ slide.vote_average }}</span>
                            <span>{{ (slide.release_date or '')[:4] }}</span>
                        </div>
                    </div>
                </a>
            </div>
            {% endfor %}
        </div>
        <div class="swiper-pagination"></div>
    </div>
    {% endif %}

    <div class="flex flex-wrap justify-center gap-3 mb-8">
        <a href="/" class="px-5 py-2 rounded-full text-sm font-semibold {{ 'bg-white text-black' if not request.args.get('type') else 'bg-zinc-800 text-gray-300' }}">All</a>
        <a href="/?type=movie" class="px-5 py-2 rounded-full text-sm font-semibold {{ 'bg-white text-black' if request.args.get('type') == 'movie' else 'bg-zinc-800 text-gray-300' }}">Movies</a>
        <a href="/?type=series" class="px-5 py-2 rounded-full text-sm font-semibold {{ 'bg-white text-black' if request.args.get('type') == 'series' else 'bg-zinc-800 text-gray-300' }}">Series</a>
    </div>

    <div class="grid grid-cols-2 md:grid-cols-4 lg:grid-cols-5 gap-4 md:gap-6">
        {% for movie in movies %}
        <a href="{{ url_for('movie_detail', movie_id=movie._id) }}" class="group relative block bg-card rounded-xl overflow-hidden hover:-translate-y-2 transition-all duration-300 border border-white/5">
            <div class="aspect-[2/3] overflow-hidden relative">
                <img src="{{ movie.poster or 'https://via.placeholder.com/300x450' }}" alt="{{ movie.title }}" class="w-full h-full object-cover group-hover:scale-110 transition duration-500">
                <div class="absolute top-2 right-2 bg-black/60 backdrop-blur-md text-yellow-400 text-xs font-bold px-2 py-1 rounded"><i class="fas fa-star"></i> {{ movie.vote_average }}</div>
            </div>
            <div class="p-3">
                <h3 class="text-white font-semibold truncate group-hover:text-primary transition">{{ movie.title }}</h3>
                <div class="flex justify-between items-center mt-1 text-xs text-gray-400">
                    <span>{{ (movie.release_date or 'N/A')[:4] }}</span>
                    <span class="border border-zinc-600 px-1 rounded">{{ movie.type|upper if movie.type else 'MOVIE' }}</span>
                </div>
            </div>
        </a>
        {% endfor %}
    </div>

    <div class="flex justify-center mt-12 gap-4">
        {% if page > 1 %}<a href="/?page={{ page-1 }}" class="px-6 py-2 bg-zinc-800 hover:bg-primary rounded-full transition">Previous</a>{% endif %}
        {% if has_next %}<a href="/?page={{ page+1 }}" class="px-6 py-2 bg-zinc-800 hover:bg-primary rounded-full transition">Next</a>{% endif %}
    </div>
</div>
<script src="https://cdn.jsdelivr.net/npm/swiper@11/swiper-bundle.min.js"></script>
<script>
    var swiper = new Swiper(".mySwiper", { loop: true, autoplay: { delay: 4000 }, pagination: { el: ".swiper-pagination", clickable: true } });
</script>
</body>
</html>
"""

detail_template = """
<!DOCTYPE html>
<html lang="en" class="dark">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{{ movie.title }} - Download</title>
    <script src="https://cdn.tailwindcss.com"></script>
    <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.0/css/all.min.css">
    <link href="https://fonts.googleapis.com/css2?family=Outfit:wght@300;400;600;700&display=swap" rel="stylesheet">
    <script>
        tailwind.config = { theme: { extend: { colors: { primary: '#8b5cf6', dark: '#09090b', card: '#18181b' }, fontFamily: { sans: ['Outfit', 'sans-serif'] } } } }
    </script>
    <style>body { background-color: #09090b; color: #fff; } .glass { background: rgba(24, 24, 27, 0.6); backdrop-filter: blur(12px); }</style>
</head>
<body class="antialiased min-h-screen">
<div class="fixed top-0 left-0 w-full h-[50vh] z-0">
    <img src="{{ movie.backdrop or movie.poster }}" class="w-full h-full object-cover opacity-30 mask-image-gradient">
    <div class="absolute inset-0 bg-gradient-to-b from-dark/30 via-dark/80 to-dark"></div>
</div>
<nav class="fixed top-0 w-full z-50 px-6 py-4 flex justify-between items-center">
    <a href="/" class="w-10 h-10 rounded-full bg-white/10 backdrop-blur flex items-center justify-center hover:bg-primary hover:text-white transition"><i class="fas fa-arrow-left"></i></a>
</nav>
<div class="relative z-10 max-w-5xl mx-auto px-4 pt-32 pb-20">
    <div class="flex flex-col md:flex-row gap-8 items-start">
        <div class="w-full md:w-72 flex-shrink-0 mx-auto md:mx-0">
            <div class="rounded-xl overflow-hidden shadow-2xl shadow-primary/20 border-2 border-white/5 relative">
                <img src="{{ movie.poster }}" class="w-full h-auto object-cover">
            </div>
        </div>
        <div class="flex-1 w-full">
            <h1 class="text-4xl md:text-5xl font-bold mb-4 leading-tight">{{ movie.title }}</h1>
            <div class="flex flex-wrap items-center gap-3 text-sm text-gray-300 mb-6">
                <span class="bg-white/10 px-3 py-1 rounded"><i class="fas fa-star mr-2 text-yellow-400"></i> {{ movie.vote_average }}</span>
                <span class="bg-white/10 px-3 py-1 rounded">{{ (movie.release_date or 'N/A')[:4] }}</span>
                <span class="bg-primary/20 text-primary border border-primary/30 px-3 py-1 rounded">{{ movie.language }}</span>
            </div>
            <p class="text-gray-400 leading-relaxed mb-8 text-lg font-light">{{ movie.overview }}</p>
            <div class="glass rounded-2xl p-6 md:p-8">
                <h3 class="text-xl font-bold mb-6 border-b border-white/10 pb-3 flex items-center gap-2">
                    <i class="fas fa-cloud-download-alt text-primary"></i> Download / Watch
                </h3>
                {% if movie.files %}
                    <div class="grid gap-3">
                        {% for file in movie.files|reverse %}
                        <a href="https://t.me/{{ BOT_USERNAME }}?start={{ file.unique_code }}" target="_blank" class="flex items-center justify-between bg-zinc-800/50 hover:bg-primary hover:scale-[1.01] border border-white/5 p-4 rounded-xl transition-all duration-300 group">
                            <div class="flex items-center gap-4">
                                <div class="w-10 h-10 rounded-full bg-white/10 flex items-center justify-center group-hover:bg-white group-hover:text-primary transition"><i class="fas fa-play"></i></div>
                                <div>
                                    <h4 class="font-bold text-white">{{ file.episode_label }}</h4>
                                    <p class="text-xs text-gray-400 group-hover:text-white/80">{{ file.quality }} • {{ file.size }}</p>
                                </div>
                            </div>
                            <div class="text-primary group-hover:text-white"><i class="fas fa-download text-lg"></i></div>
                        </a>
                        {% endfor %}
                    </div>
                {% else %}
                    <div class="text-center py-6">
                        <p class="text-gray-400 mb-4">Files are not uploaded yet.</p>
                        <a href="{{ request_channel }}" class="inline-flex items-center gap-2 bg-red-600 hover:bg-red-700 text-white px-8 py-3 rounded-full font-bold transition">
                            <i class="fas fa-paper-plane"></i> Request to Admin
                        </a>
                    </div>
                {% endif %}
            </div>
        </div>
    </div>
</div>
</body>
</html>
"""

# ================================
#        ADMIN PANEL TEMPLATES (Bootstrap)
# ================================

admin_base = """
<!DOCTYPE html>
<html lang="en" data-bs-theme="dark">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Admin Panel</title>
    <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/css/bootstrap.min.css" rel="stylesheet">
    <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.0/css/all.min.css">
    <style>
        body { background-color: #0f1012; font-family: sans-serif; }
        .sidebar { height: 100vh; position: fixed; top: 0; left: 0; width: 220px; background: #191b1f; padding-top: 20px; border-right: 1px solid #333; }
        .sidebar a { padding: 12px 20px; display: block; color: #aaa; text-decoration: none; transition: 0.2s; }
        .sidebar a:hover, .sidebar a.active { color: #fff; background: #E50914; }
        .main-content { margin-left: 220px; padding: 20px; }
        @media (max-width: 768px) { .sidebar { width: 60px; } .sidebar span { display: none; } .main-content { margin-left: 60px; } }
    </style>
</head>
<body>
<div class="sidebar">
    <h4 class="text-center text-danger fw-bold mb-4">ADMIN</h4>
    <a href="/admin" class="{{ 'active' if active == 'dashboard' else '' }}"><i class="fas fa-home"></i> <span>Dashboard</span></a>
    <a href="/admin/settings" class="{{ 'active' if active == 'settings' else '' }}"><i class="fas fa-cog"></i> <span>Settings</span></a>
    <a href="/" target="_blank"><i class="fas fa-eye"></i> <span>View Site</span></a>
</div>
<div class="main-content">
    <!-- CONTENT_GOES_HERE -->
</div>
<script src="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/js/bootstrap.bundle.min.js"></script>
</body>
</html>
"""

admin_dashboard = """
<div class="d-flex justify-content-between mb-4">
    <h3>All Movies</h3>
    <form class="d-flex" method="GET"><input class="form-control me-2" name="q" placeholder="Search..." value="{{ q }}"><button class="btn btn-outline-light">Search</button></form>
</div>
<div class="row">
    {% for movie in movies %}
    <div class="col-6 col-md-3 mb-4">
        <div class="card h-100 bg-dark border-secondary">
            <img src="{{ movie.poster }}" class="card-img-top" style="height: 200px; object-fit: cover;">
            <div class="card-body p-2">
                <h6 class="text-truncate">{{ movie.title }}</h6>
                <div class="d-flex gap-2 mt-2">
                    <a href="/admin/movie/edit/{{ movie._id }}" class="btn btn-sm btn-primary w-100">Edit</a>
                    <a href="/admin/movie/delete/{{ movie._id }}" onclick="return confirm('Delete?')" class="btn btn-sm btn-danger"><i class="fas fa-trash"></i></a>
                </div>
            </div>
        </div>
    </div>
    {% endfor %}
</div>
<div class="d-flex justify-content-center mt-3">
    {% if page > 1 %}<a href="?page={{ page-1 }}" class="btn btn-secondary me-2">Prev</a>{% endif %}
    <a href="?page={{ page+1 }}" class="btn btn-secondary">Next</a>
</div>
"""

admin_edit = """
<div class="container-fluid">
    <div class="d-flex justify-content-between align-items-center mb-4">
        <h3>Edit: <span class="text-primary">{{ movie.title }}</span></h3>
        <a href="/admin" class="btn btn-secondary btn-sm">Back</a>
    </div>
    <div class="row">
        <!-- TMDB Smart Fetch Column -->
        <div class="col-md-4 mb-4">
            <div class="card p-3 mb-3 border-primary shadow-sm bg-dark">
                <h6 class="text-primary"><i class="fas fa-magic"></i> Correct Wrong Data</h6>
                <div class="form-text mb-2 text-muted">Paste TMDB ID, Link or Correct Name:</div>
                <div class="input-group">
                    <input type="text" id="smartInput" class="form-control" placeholder="e.g. 12345 or Naruto">
                    <button class="btn btn-primary" type="button" onclick="smartFetch()">Fetch</button>
                </div>
                <div id="tmdbResults" class="mt-3"></div>
            </div>
            <div class="text-center"><img src="{{ movie.poster }}" class="img-fluid rounded" style="max-height: 300px;"></div>
        </div>

        <!-- Edit Form -->
        <div class="col-md-8">
            <div class="card p-4 bg-dark border-secondary">
                <form method="POST">
                    <div class="mb-3">
                        <label>Title</label>
                        <input type="text" name="title" class="form-control" value="{{ movie.title }}" required>
                    </div>
                    <div class="row">
                        <div class="col-md-6 mb-3">
                            <label>Type</label>
                            <select name="type" class="form-select">
                                <option value="movie" {{ 'selected' if movie.type == 'movie' else '' }}>Movie</option>
                                <option value="series" {{ 'selected' if movie.type == 'series' else '' }}>Series</option>
                            </select>
                        </div>
                        <div class="col-md-6 mb-3">
                            <label>Language</label>
                            <input type="text" name="language" class="form-control" value="{{ movie.language }}">
                        </div>
                    </div>
                    <div class="mb-3">
                        <label>Overview</label>
                        <textarea name="overview" class="form-control" rows="4">{{ movie.overview }}</textarea>
                    </div>
                    <div class="row">
                        <div class="col-md-6 mb-3"><label>Poster URL</label><input type="text" name="poster" class="form-control" value="{{ movie.poster }}"></div>
                        <div class="col-md-6 mb-3"><label>Backdrop URL</label><input type="text" name="backdrop" class="form-control" value="{{ movie.backdrop }}"></div>
                    </div>
                    <div class="row">
                        <div class="col-md-6 mb-3"><label>Rating</label><input type="text" name="vote_average" class="form-control" value="{{ movie.vote_average }}"></div>
                        <div class="col-md-6 mb-3"><label>Year</label><input type="text" name="release_date" class="form-control" value="{{ movie.release_date }}"></div>
                    </div>
                    <button type="submit" class="btn btn-success w-100">Update Details</button>
                </form>
            </div>
        </div>
    </div>
</div>

<script>
    function smartFetch() {
        let query = document.getElementById('smartInput').value.trim();
        const resultDiv = document.getElementById('tmdbResults');
        if(!query) return;
        
        resultDiv.innerHTML = '<div class="spinner-border text-primary spinner-border-sm"></div> Searching...';
        
        fetch('/admin/api/tmdb?q=' + encodeURIComponent(query))
        .then(r => r.json())
        .then(data => {
            if(data.error || !data.results.length) {
                resultDiv.innerHTML = '<div class="text-danger small">No results found.</div>';
                return;
            }
            let html = '<div class="list-group">';
            data.results.forEach(item => {
                let title = item.title || item.name;
                let date = item.release_date || item.first_air_date || 'N/A';
                let poster = item.poster_path ? 'https://image.tmdb.org/t/p/w92' + item.poster_path : '';
                let cleanItem = JSON.stringify(item).replace(/'/g, "&#39;").replace(/"/g, "&quot;");

                html += `<button type="button" class="list-group-item list-group-item-action d-flex align-items-center gap-2 p-1" onclick='fillForm(${cleanItem})'>
                    <img src="${poster}" width="40">
                    <div class="small text-truncate">${title} (${date.substring(0,4)})</div>
                </button>`;
            });
            html += '</div>';
            resultDiv.innerHTML = html;
        });
    }

    function fillForm(data) {
        document.querySelector('input[name="title"]').value = data.title || data.name;
        document.querySelector('textarea[name="overview"]').value = data.overview || '';
        document.querySelector('input[name="release_date"]').value = data.release_date || data.first_air_date || '';
        document.querySelector('input[name="vote_average"]').value = data.vote_average || '';
        if(data.poster_path) document.querySelector('input[name="poster"]').value = 'https://image.tmdb.org/t/p/w500' + data.poster_path;
        if(data.backdrop_path) document.querySelector('input[name="backdrop"]').value = 'https://image.tmdb.org/t/p/original' + data.backdrop_path;
        if(data.media_type === 'tv') document.querySelector('select[name="type"]').value = 'series';
        document.getElementById('tmdbResults').innerHTML = '<div class="alert alert-success p-1 small">Data Applied! Click Update.</div>';
    }
</script>
"""

# ================================
#        FLASK ROUTES
# ================================

@app.route('/')
def home():
    page = int(request.args.get('page', 1))
    per_page = 20
    query = request.args.get('q', '').strip()
    type_filter = request.args.get('type', '').strip()
    
    db_query = {}
    if query: db_query["title"] = {"$regex": query, "$options": "i"}
    if type_filter: db_query["type"] = type_filter

    total_movies = movies.count_documents(db_query)
    movie_list = list(movies.find(db_query).sort([('updated_at', -1)]).skip((page-1)*per_page).limit(per_page))
    
    slider_movies = []
    if not query and not type_filter and page == 1:
        slider_movies = list(movies.find({"backdrop": {"$ne": None}}).sort([('created_at', -1)]).limit(5))

    return render_template_string(index_template, movies=movie_list, slider_movies=slider_movies, page=page, has_next=(page*per_page < total_movies))

@app.route('/movie/<movie_id>')
def movie_detail(movie_id):
    try:
        movie = movies.find_one({"_id": ObjectId(movie_id)})
        if not movie: return "Not Found", 404
        return render_template_string(detail_template, movie=movie)
    except: return "Invalid ID", 400

# API Proxy
@app.route('/api/shorten')
def shorten_link_proxy():
    original_url = request.args.get('url')
    api_key = request.args.get('api')
    domain = request.args.get('domain')
    if not original_url or not api_key or not domain: return jsonify({'error': 'Missing Params'})
    try:
        api_url = f"https://{domain}/api?api={api_key}&url={urllib.parse.quote(original_url)}"
        return jsonify(requests.get(api_url).json())
    except Exception as e: return jsonify({'error': str(e)})

# --- ADMIN ROUTES ---

@app.route('/admin')
def admin_home():
    if not check_auth(): return Response('Login Required', 401, {'WWW-Authenticate': 'Basic realm="Login Required"'})
    page = int(request.args.get('page', 1))
    q = request.args.get('q', '')
    filter_q = {'title': {'$regex': q, '$options': 'i'}} if q else {}
    movie_list = list(movies.find(filter_q).sort('_id', -1).skip((page-1)*20).limit(20))
    full_html = admin_base.replace('<!-- CONTENT_GOES_HERE -->', admin_dashboard)
    return render_template_string(full_html, movies=movie_list, page=page, q=q, active='dashboard')

@app.route('/admin/movie/edit/<movie_id>', methods=['GET', 'POST'])
def admin_edit_movie(movie_id):
    if not check_auth(): return Response('Login Required', 401)
    movie = movies.find_one({"_id": ObjectId(movie_id)})
    
    if request.method == 'POST':
        update_data = {
            "title": request.form.get("title"),
            "language": request.form.get("language"),
            "overview": request.form.get("overview"),
            "poster": request.form.get("poster"),
            "backdrop": request.form.get("backdrop"),
            "release_date": request.form.get("release_date"),
            "vote_average": request.form.get("vote_average"),
            "type": request.form.get("type"),
            "updated_at": datetime.utcnow()
        }
        movies.update_one({"_id": ObjectId(movie_id)}, {"$set": update_data})
        return redirect(url_for('admin_home'))
        
    full_html = admin_base.replace('<!-- CONTENT_GOES_HERE -->', admin_edit)
    return render_template_string(full_html, movie=movie, active='dashboard')

@app.route('/admin/movie/delete/<movie_id>')
def admin_delete_movie(movie_id):
    if not check_auth(): return Response('Login Required', 401)
    movies.delete_one({"_id": ObjectId(movie_id)})
    return redirect(url_for('admin_home'))

@app.route('/admin/api/tmdb')
def api_tmdb_search():
    if not check_auth(): return jsonify({'error': 'Unauthorized'}), 401
    query = request.args.get('q', '').strip()
    if not query or not TMDB_API_KEY: return jsonify({'error': 'No query'})
    
    # Check if TMDB ID
    if query.isdigit():
        url = f"https://api.themoviedb.org/3/movie/{query}?api_key={TMDB_API_KEY}"
        resp = requests.get(url)
        if resp.status_code == 200: return jsonify({'results': [resp.json()]})
        
    # Search
    url = f"https://api.themoviedb.org/3/search/multi?api_key={TMDB_API_KEY}&query={requests.utils.quote(query)}"
    return jsonify(requests.get(url).json())

if __name__ == '__main__':
    if WEBSITE_URL and BOT_TOKEN:
        try: requests.get(f"https://api.telegram.org/bot{BOT_TOKEN}/setWebhook?url={WEBSITE_URL.rstrip('/')}/webhook/{BOT_TOKEN}")
        except: pass
    app.run(host='0.0.0.0', port=int(os.environ.get("PORT", 5000)), debug=True)
