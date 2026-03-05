import os, json, uuid, threading, subprocess, re, time
from flask import Flask, request, jsonify, render_template, redirect, send_from_directory
import requests

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', 'viralpro2024secret')

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
TEMP_DIR = os.path.join(BASE_DIR, 'temp')
OUTPUT_DIR = os.path.join(BASE_DIR, 'outputs')
CONFIG_FILE = os.path.join(BASE_DIR, 'config.json')
os.makedirs(TEMP_DIR, exist_ok=True)
os.makedirs(OUTPUT_DIR, exist_ok=True)

jobs = {}

# ── Config ────────────────────────────────────────────────────────────────────
def load_config():
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE) as f:
            return json.load(f)
    return {}

def save_config(data):
    cfg = load_config()
    cfg.update(data)
    with open(CONFIG_FILE, 'w') as f:
        json.dump(cfg, f)

def get_video_id(url):
    m = re.search(r'(?:v=|youtu\.be/|shorts/)([a-zA-Z0-9_-]{11})', url)
    return m.group(1) if m else None

# ── yt-dlp download ───────────────────────────────────────────────────────────
def download_section(url, start, end, out_path, job_id=None):
    """yt-dlp দিয়ে শুধু দরকারি অংশ download"""
    if job_id:
        jobs[job_id]['message'] = f'Downloading {int(start)}s-{int(end)}s...'

    clients = ['android_vr', 'android_testsuite', 'android', 'web']

    for client in clients:
        if job_id:
            jobs[job_id]['message'] = f'Trying {client} client...'
        cmd = [
            'yt-dlp',
            '--extractor-args', f'youtube:player_client={client}',
            '--download-sections', f'*{start}-{end}',
            '--format', 'bestvideo[ext=mp4][height<=1080]+bestaudio[ext=m4a]/best[ext=mp4]/best',
            '--merge-output-format', 'mp4',
            '--no-playlist', '--no-warnings',
            '-o', out_path, url
        ]
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
            if os.path.exists(out_path) and os.path.getsize(out_path) > 10000:
                return True, None
        except subprocess.TimeoutExpired:
            continue
        except Exception as e:
            continue

    # Last resort: no section limit
    if job_id:
        jobs[job_id]['message'] = 'Trying full download fallback...'
    cmd = ['yt-dlp', '--format', 'worst[ext=mp4]/worst',
           '--no-playlist', '-o', out_path, url]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        if os.path.exists(out_path) and os.path.getsize(out_path) > 10000:
            return True, None
        return False, r.stderr[-400:] if r.stderr else 'Download failed'
    except Exception as e:
        return False, str(e)

# ── Transcribe ────────────────────────────────────────────────────────────────
def transcribe_url(url, groq_key, job_id):
    jobs[job_id]['message'] = 'Audio download করছি...'
    audio_path = os.path.join(TEMP_DIR, f"{job_id}_aud")

    cmd = [
        'yt-dlp',
        '--extractor-args', 'youtube:player_client=android_vr',
        '--download-sections', '*0-300',
        '--format', 'bestaudio[ext=m4a]/bestaudio',
        '--extract-audio', '--audio-format', 'mp3', '--audio-quality', '64K',
        '--no-playlist', '--no-warnings',
        '-o', audio_path + '.%(ext)s', url
    ]
    try:
        subprocess.run(cmd, capture_output=True, timeout=120)
    except:
        pass

    found = None
    for ext in ['mp3', 'm4a', 'webm', 'ogg', 'opus']:
        p = f"{audio_path}.{ext}"
        if os.path.exists(p):
            found = p
            break

    if not found:
        return None

    jobs[job_id]['message'] = 'Groq Whisper দিয়ে transcribe করছি...'
    try:
        ext = found.split('.')[-1]
        with open(found, 'rb') as f:
            r = requests.post(
                'https://api.groq.com/openai/v1/audio/transcriptions',
                headers={'Authorization': f'Bearer {groq_key}'},
                files={'file': (f'audio.{ext}', f, f'audio/{ext}')},
                data={'model': 'whisper-large-v3', 'response_format': 'verbose_json',
                      'timestamp_granularities[]': 'segment'},
                timeout=300
            )
        if r.status_code == 200:
            return r.json()
    except:
        pass
    return None

# ── Gemini Analysis ───────────────────────────────────────────────────────────
def analyze_viral(transcript_data, num_clips, gemini_key, job_id):
    jobs[job_id]['message'] = f'Gemini AI দিয়ে {num_clips}টা viral moment খুঁজছি...'
    transcript_text = ""
    if transcript_data and 'segments' in transcript_data:
        for seg in transcript_data['segments']:
            transcript_text += f"[{seg['start']:.1f}s-{seg['end']:.1f}s]: {seg['text']}\n"

    if not gemini_key or not transcript_text:
        return fallback_segments(transcript_data, num_clips)

    prompt = f"""You are a viral YouTube Shorts expert. Find exactly {num_clips} most viral moments (30-90 seconds each).

TRANSCRIPT:
{transcript_text[:8000]}

Return ONLY valid JSON array no markdown:
[{{"rank":1,"start_time":45.0,"end_time":98.0,"hook":"hook text","why_viral":"reason","title":"🔥 title max 90 chars","description":"150 chars","hashtags":["#shorts","#viral","#trending"]}}]"""

    try:
        r = requests.post(
            f'https://generativelanguage.googleapis.com/v1beta/models/gemini-1.5-flash:generateContent?key={gemini_key}',
            json={'contents': [{'parts': [{'text': prompt}]}],
                  'generationConfig': {'temperature': 0.7, 'maxOutputTokens': 2000}},
            timeout=60
        )
        if r.status_code == 200:
            text = r.json()['candidates'][0]['content']['parts'][0]['text']
            text = re.sub(r'```json|```', '', text).strip()
            return json.loads(text)
    except:
        pass
    return fallback_segments(transcript_data, num_clips)

def fallback_segments(transcript_data, num_clips):
    total = 300
    if transcript_data and 'segments' in transcript_data:
        segs = transcript_data.get('segments', [])
        if segs: total = segs[-1]['end']
    return [{
        'rank': i+1,
        'start_time': round(i * (total/num_clips), 1),
        'end_time': round(min((i+1) * (total/num_clips), total), 1),
        'hook': f'Segment {i+1}', 'why_viral': 'Auto-selected',
        'title': f'🔥 Amazing Clip #{i+1}',
        'description': 'Watch this!',
        'hashtags': ['#shorts', '#viral', '#trending']
    } for i in range(num_clips)]

# ── Crop 9:16 ─────────────────────────────────────────────────────────────────
def crop_shorts(video_path, out_name, text=None):
    out_path = os.path.join(OUTPUT_DIR, f"{out_name}.mp4")
    probe = subprocess.run(
        ['ffprobe', '-v', 'quiet', '-print_format', 'json', '-show_streams', video_path],
        capture_output=True, text=True)
    w, h = 1920, 1080
    try:
        for s in json.loads(probe.stdout).get('streams', []):
            if s.get('codec_type') == 'video':
                w, h = s['width'], s['height']
                break
    except: pass

    crop_w = min(w, int(h * 9/16))
    crop_h = int(crop_w * 16/9)
    if crop_h > h:
        crop_h = h; crop_w = int(crop_h * 9/16)
    x = (w - crop_w)//2; y = (h - crop_h)//2

    vf = f"crop={crop_w}:{crop_h}:{x}:{y},scale=1080:1920:force_original_aspect_ratio=decrease,pad=1080:1920:(ow-iw)/2:(oh-ih)/2:black"
    if text:
        safe = text.replace("'", "\\'").replace(":", "\\:")
        vf += f",drawtext=text='{safe}':fontsize=52:fontcolor=white:x=(w-text_w)/2:y=h-180:box=1:boxcolor=black@0.75:boxborderw=12"

    cmd = ['ffmpeg', '-i', video_path, '-vf', vf,
           '-c:v', 'libx264', '-preset', 'fast', '-crf', '23',
           '-c:a', 'aac', '-b:a', '128k', '-y', out_path]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    if os.path.exists(out_path) and os.path.getsize(out_path) > 1000:
        return out_path
    return None

# ── YouTube Upload ────────────────────────────────────────────────────────────
def youtube_upload(video_path, title, description, tags, access_token):
    meta = {
        'snippet': {'title': title[:100], 'description': description[:5000],
                    'tags': tags[:10] if isinstance(tags, list) else [], 'categoryId': '22'},
        'status': {'privacyStatus': 'public', 'selfDeclaredMadeForKids': False}
    }
    try:
        init = requests.post(
            'https://www.googleapis.com/upload/youtube/v3/videos?uploadType=resumable&part=snippet,status',
            headers={'Authorization': f'Bearer {access_token}',
                     'Content-Type': 'application/json', 'X-Upload-Content-Type': 'video/mp4'},
            json=meta, timeout=30)
        if init.status_code not in [200, 201]:
            return None, f"Init failed: {init.text}"
        with open(video_path, 'rb') as f:
            up = requests.put(init.headers['Location'],
                              headers={'Content-Type': 'video/mp4'}, data=f, timeout=600)
        if up.status_code in [200, 201]:
            return up.json().get('id'), None
        return None, up.text
    except Exception as e:
        return None, str(e)

# ── Routes ────────────────────────────────────────────────────────────────────
@app.route('/test')
def test_tools():
    results = []
    r = subprocess.run(['ffmpeg', '-version'], capture_output=True, text=True)
    results.append("✅ FFmpeg OK" if r.returncode == 0 else "❌ FFmpeg missing")
    r = subprocess.run(['yt-dlp', '--version'], capture_output=True, text=True)
    results.append(f"✅ yt-dlp {r.stdout.strip()}" if r.returncode == 0 else "❌ yt-dlp missing")

    results.append("\nyt-dlp 3sec download test...")
    test_path = '/tmp/yt_test.mp4'
    for client in ['android_vr', 'android_testsuite', 'web']:
        cmd = ['yt-dlp', '--extractor-args', f'youtube:player_client={client}',
               '--download-sections', '*0-3', '--format', 'best[ext=mp4]/best',
               '--no-playlist', '-o', test_path,
               'https://www.youtube.com/watch?v=dQw4w9WgXcQ']
        try:
            res = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
            if os.path.exists(test_path) and os.path.getsize(test_path) > 1000:
                results.append(f"✅ {client} works! ({os.path.getsize(test_path)//1024}KB)")
                break
            else:
                results.append(f"❌ {client}: {res.stderr[-200:]}")
        except Exception as e:
            results.append(f"❌ {client}: {e}")

    html = "<h2 style='font-family:monospace;padding:20px'>🔧 Tool Test</h2>"
    html += "<pre style='font-family:monospace;font-size:13px;padding:20px;background:#111;color:#0f0'>"
    html += "\n".join(results) + "</pre><br><a href='/'>← Back</a>"
    return html

@app.route('/')
def index():
    return render_template('index.html', config=load_config())

@app.route('/api/config', methods=['GET', 'POST'])
def api_config():
    if request.method == 'POST':
        save_config(request.json)
        return jsonify({'success': True})
    return jsonify(load_config())

@app.route('/api/process', methods=['POST'])
def api_process():
    data = request.json
    url = data.get('url', '')
    num_clips = int(data.get('num_clips', 3))
    job_id = uuid.uuid4().hex[:8]
    jobs[job_id] = {'status': 'running', 'progress': 0, 'message': 'শুরু হচ্ছে...', 'clips': []}

    def run():
        try:
            cfg = load_config()
            if not get_video_id(url):
                jobs[job_id].update({'status': 'error', 'message': 'Invalid YouTube URL!'})
                return

            jobs[job_id]['progress'] = 10
            transcript = None
            if cfg.get('groq_api_key'):
                transcript = transcribe_url(url, cfg['groq_api_key'], job_id)

            jobs[job_id]['progress'] = 40
            segments = analyze_viral(transcript, num_clips, cfg.get('gemini_api_key', ''), job_id)

            clips = []
            for i, seg in enumerate(segments[:num_clips]):
                jobs[job_id]['progress'] = 50 + int((i/num_clips) * 45)
                clip_name = f"{job_id}_clip{i+1}"
                raw_path = os.path.join(TEMP_DIR, f"{clip_name}_raw.mp4")

                ok, err = download_section(url, seg['start_time'], seg['end_time'], raw_path, job_id)
                if ok:
                    jobs[job_id]['message'] = f"Clip {i+1} crop করছি..."
                    clip_path = crop_shorts(raw_path, clip_name)
                    if clip_path:
                        seg['clip_name'] = clip_name
                        seg['preview_url'] = f'/outputs/{clip_name}.mp4'
                        clips.append(seg)
                        try: os.remove(raw_path)
                        except: pass
                else:
                    jobs[job_id]['message'] = f"Clip {i+1} failed: {(err or '')[:100]}"
                    time.sleep(1)

            if clips:
                jobs[job_id].update({'status': 'done', 'progress': 100,
                                     'message': f'✅ {len(clips)}টা clip তৈরি!', 'clips': clips})
            else:
                jobs[job_id].update({'status': 'error',
                                     'message': 'yt-dlp blocked! /test এ গিয়ে দেখো।'})
        except Exception as e:
            jobs[job_id].update({'status': 'error', 'message': str(e)})

    threading.Thread(target=run, daemon=True).start()
    return jsonify({'job_id': job_id})

@app.route('/api/manual-crop', methods=['POST'])
def api_manual_crop():
    data = request.json
    url = data.get('url', '')
    start = float(data.get('start', 0))
    end = float(data.get('end', 60))
    text = data.get('text', '')
    if not url:
        return jsonify({'error': 'URL দাও'}), 400

    clip_name = f"manual_{uuid.uuid4().hex[:8]}"
    raw_path = os.path.join(TEMP_DIR, f"{clip_name}_raw.mp4")
    job_id = uuid.uuid4().hex[:8]
    jobs[job_id] = {'status': 'running', 'progress': 20, 'message': 'Downloading...'}

    def run():
        ok, err = download_section(url, start, end, raw_path, job_id)
        if ok:
            jobs[job_id]['message'] = 'Cropping...'
            clip_path = crop_shorts(raw_path, clip_name, text if text else None)
            if clip_path:
                jobs[job_id].update({'status': 'done', 'progress': 100, 'message': 'Done!',
                                     'preview_url': f'/outputs/{clip_name}.mp4', 'clip_name': clip_name})
                try: os.remove(raw_path)
                except: pass
            else:
                jobs[job_id].update({'status': 'error', 'message': 'Crop failed'})
        else:
            jobs[job_id].update({'status': 'error', 'message': f'Download failed: {err}'})

    threading.Thread(target=run, daemon=True).start()
    return jsonify({'job_id': job_id})

@app.route('/api/job/<job_id>')
def api_job(job_id):
    return jsonify(jobs.get(job_id, {'status': 'not_found'}))

@app.route('/api/upload', methods=['POST'])
def api_upload():
    data = request.json
    cfg = load_config()
    access_token = cfg.get('yt_access_token', '')
    if not access_token:
        return jsonify({'error': 'YouTube connected নেই'}), 400
    video_path = os.path.join(OUTPUT_DIR, f"{data['clip_name']}.mp4")
    if not os.path.exists(video_path):
        return jsonify({'error': 'Video file নেই'}), 400

    job_id = uuid.uuid4().hex[:8]
    jobs[job_id] = {'status': 'running', 'message': 'Upload হচ্ছে...', 'progress': 50}

    def run():
        vid_id, err = youtube_upload(video_path, data.get('title', 'Amazing Short'),
                                     data.get('description', ''), data.get('tags', []), access_token)
        if vid_id:
            jobs[job_id].update({'status': 'done', 'progress': 100,
                                 'message': f'✅ https://youtube.com/shorts/{vid_id}', 'video_id': vid_id})
        else:
            jobs[job_id].update({'status': 'error', 'message': err})

    threading.Thread(target=run, daemon=True).start()
    return jsonify({'job_id': job_id})

@app.route('/api/generate-meta', methods=['POST'])
def api_generate_meta():
    data = request.json
    cfg = load_config()
    gemini_key = cfg.get('gemini_api_key', '')
    hook = data.get('hook', '')
    why = data.get('why_viral', '')
    if not gemini_key:
        return jsonify({'title': f'🔥 {hook}', 'description': why,
                        'tags': ['#shorts', '#viral', '#trending']})
    prompt = f'Create viral YouTube Shorts metadata. Hook: {hook}. Why viral: {why}. Return ONLY JSON: {{"title":"emoji title 90 chars","description":"200 chars","tags":["#shorts","#viral","#trending","#youtube","#fyp"]}}'
    try:
        r = requests.post(
            f'https://generativelanguage.googleapis.com/v1beta/models/gemini-1.5-flash:generateContent?key={gemini_key}',
            json={'contents': [{'parts': [{'text': prompt}]}]}, timeout=30)
        if r.status_code == 200:
            text = r.json()['candidates'][0]['content']['parts'][0]['text']
            return jsonify(json.loads(re.sub(r'```json|```', '', text).strip()))
    except: pass
    return jsonify({'title': f'🔥 {hook}', 'description': why, 'tags': ['#shorts', '#viral']})

@app.route('/api/yt-auth-url')
def yt_auth_url():
    cfg = load_config()
    if not cfg.get('yt_client_id'):
        return jsonify({'error': 'YouTube Client ID নেই'}), 400
    scope = 'https://www.googleapis.com/auth/youtube.upload'
    redirect_uri = request.host_url.rstrip('/') + '/api/yt-callback'
    url = (f"https://accounts.google.com/o/oauth2/v2/auth"
           f"?client_id={cfg['yt_client_id']}&redirect_uri={redirect_uri}"
           f"&response_type=code&scope={requests.utils.quote(scope)}"
           f"&access_type=offline&prompt=consent")
    return jsonify({'auth_url': url})

@app.route('/api/yt-callback')
def yt_callback():
    code = request.args.get('code')
    cfg = load_config()
    redirect_uri = request.host_url.rstrip('/') + '/api/yt-callback'
    r = requests.post('https://oauth2.googleapis.com/token', data={
        'code': code, 'client_id': cfg.get('yt_client_id'),
        'client_secret': cfg.get('yt_client_secret'),
        'redirect_uri': redirect_uri, 'grant_type': 'authorization_code'})
    if r.status_code == 200:
        tokens = r.json()
        save_config({'yt_access_token': tokens.get('access_token'),
                     'yt_refresh_token': tokens.get('refresh_token')})
        return redirect('/?yt=connected')
    return f"Error: {r.text}", 400

@app.route('/api/yt-disconnect', methods=['POST'])
def yt_disconnect():
    cfg = load_config()
    cfg.pop('yt_access_token', None)
    cfg.pop('yt_refresh_token', None)
    save_config(cfg)
    return jsonify({'success': True})

@app.route('/outputs/<path:filename>')
def serve_output(filename):
    return send_from_directory(OUTPUT_DIR, filename)

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 8080)), debug=False)
