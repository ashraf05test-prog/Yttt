import os, json, uuid, threading, subprocess, re, time
from flask import Flask, request, jsonify, render_template, redirect, send_from_directory, session
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

# ── Config ──────────────────────────────────────────────────────────────────
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

# ── Video ID ─────────────────────────────────────────────────────────────────
def get_video_id(url):
    m = re.search(r'(?:v=|youtu\.be/|shorts/)([a-zA-Z0-9_-]{11})', url)
    return m.group(1) if m else None

# ── Piped API ────────────────────────────────────────────────────────────────
PIPED_INSTANCES = [
    "https://pipedapi.kavin.rocks",
    "https://pipedapi.reallyaweso.me",
    "https://piped-api.privacy.com.de",
    "https://api.piped.yt",
]

def get_stream_url(video_id):
    for instance in PIPED_INSTANCES:
        try:
            r = requests.get(f"{instance}/streams/{video_id}", timeout=10,
                           headers={"User-Agent": "Mozilla/5.0"})
            if r.status_code == 200:
                data = r.json()
                # Find best combined stream first
                for s in data.get('videoStreams', []):
                    if 'mp4' in s.get('mimeType', '') and not s.get('videoOnly', True):
                        return s['url'], data.get('audioStreams', [{}])[0].get('url', ''), instance
                # Fallback: videoOnly best quality
                best = None
                for s in data.get('videoStreams', []):
                    if 'mp4' in s.get('mimeType', ''):
                        if not best or int(s.get('bitrate', 0)) > int(best.get('bitrate', 0)):
                            best = s
                if best:
                    audio = data.get('audioStreams', [{}])[0].get('url', '')
                    return best['url'], audio, instance
        except:
            continue
    return None, None, None

# ── Transcribe ───────────────────────────────────────────────────────────────
def transcribe(video_path, groq_key, job_id):
    jobs[job_id]['message'] = 'Audio extract করছি...'
    audio_path = video_path.replace('.mp4', '.mp3')
    subprocess.run(['ffmpeg', '-i', video_path, '-vn', '-ar', '16000',
                    '-ac', '1', '-b:a', '64k', '-y', audio_path],
                   capture_output=True, timeout=120)
    if not os.path.exists(audio_path):
        return None
    jobs[job_id]['message'] = 'Groq Whisper দিয়ে transcribe করছি...'
    try:
        with open(audio_path, 'rb') as f:
            r = requests.post(
                'https://api.groq.com/openai/v1/audio/transcriptions',
                headers={'Authorization': f'Bearer {groq_key}'},
                files={'file': ('audio.mp3', f, 'audio/mpeg')},
                data={'model': 'whisper-large-v3', 'response_format': 'verbose_json',
                      'timestamp_granularities[]': 'segment'},
                timeout=300
            )
        if r.status_code == 200:
            return r.json()
    except:
        pass
    return None

# ── Gemini Analysis ──────────────────────────────────────────────────────────
def analyze_viral(transcript_data, num_clips, gemini_key, job_id):
    jobs[job_id]['message'] = f'Gemini AI দিয়ে {num_clips}টা viral moment খুঁজছি...'
    
    transcript_text = ""
    if transcript_data and 'segments' in transcript_data:
        for seg in transcript_data['segments']:
            transcript_text += f"[{seg['start']:.1f}s-{seg['end']:.1f}s]: {seg['text']}\n"

    if not gemini_key or not transcript_text:
        return fallback_segments(transcript_data, num_clips)

    prompt = f"""You are a viral YouTube Shorts expert. Analyze this transcript and find {num_clips} most viral moments (30-90 seconds each).

TRANSCRIPT:
{transcript_text[:8000]}

Return ONLY a valid JSON array with exactly {num_clips} items:
[
  {{
    "rank": 1,
    "start_time": 45.0,
    "end_time": 98.0,
    "hook": "One line hook description",
    "why_viral": "Why this will go viral",
    "title": "Catchy YouTube Shorts title with emoji",
    "description": "Engaging description 150 chars",
    "hashtags": ["#shorts", "#viral", "#trending"]
  }}
]"""

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
        segs = transcript_data['segments']
        if segs:
            total = segs[-1]['end']
    result = []
    for i in range(num_clips):
        start = i * (total / num_clips)
        end = min(start + 60, total)
        result.append({
            'rank': i + 1, 'start_time': round(start, 1), 'end_time': round(end, 1),
            'hook': f'Segment {i+1}', 'why_viral': 'Auto-selected',
            'title': f'🔥 Amazing Clip #{i+1}',
            'description': f'Check this out! #{i+1}',
            'hashtags': ['#shorts', '#viral', '#trending']
        })
    return result

# ── Crop to 9:16 ─────────────────────────────────────────────────────────────
def crop_shorts(video_path, start, end, out_name, text=None):
    out_path = os.path.join(OUTPUT_DIR, f"{out_name}.mp4")
    
    # Get dimensions
    probe = subprocess.run(['ffprobe', '-v', 'quiet', '-print_format', 'json',
                            '-show_streams', video_path], capture_output=True, text=True)
    w, h = 1920, 1080
    try:
        info = json.loads(probe.stdout)
        for s in info.get('streams', []):
            if s.get('codec_type') == 'video':
                w, h = s['width'], s['height']
                break
    except:
        pass

    # 9:16 crop calculation
    crop_w = min(w, int(h * 9 / 16))
    crop_h = int(crop_w * 16 / 9)
    if crop_h > h:
        crop_h = h
        crop_w = int(crop_h * 9 / 16)
    x = (w - crop_w) // 2
    y = (h - crop_h) // 2

    vf = f"crop={crop_w}:{crop_h}:{x}:{y},scale=1080:1920"
    
    if text:
        safe = text.replace("'", "\\'").replace(":", "\\:")
        vf += f",drawtext=text='{safe}':fontsize=52:fontcolor=white:x=(w-text_w)/2:y=h-180:box=1:boxcolor=black@0.75:boxborderw=12"

    cmd = ['ffmpeg', '-ss', str(start), '-to', str(end), '-i', video_path,
           '-vf', vf, '-c:v', 'libx264', '-preset', 'fast', '-crf', '23',
           '-c:a', 'aac', '-b:a', '128k', '-y', out_path]
    
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    if os.path.exists(out_path) and os.path.getsize(out_path) > 1000:
        return out_path
    return None

# ── YouTube Upload ────────────────────────────────────────────────────────────
def youtube_upload(video_path, title, description, tags, access_token):
    meta = {
        'snippet': {'title': title[:100], 'description': description[:5000],
                    'tags': tags[:10] if isinstance(tags, list) else [],
                    'categoryId': '22'},
        'status': {'privacyStatus': 'public', 'selfDeclaredMadeForKids': False}
    }
    try:
        init = requests.post(
            'https://www.googleapis.com/upload/youtube/v3/videos?uploadType=resumable&part=snippet,status',
            headers={'Authorization': f'Bearer {access_token}',
                     'Content-Type': 'application/json',
                     'X-Upload-Content-Type': 'video/mp4'},
            json=meta, timeout=30
        )
        if init.status_code not in [200, 201]:
            return None, f"Init failed: {init.text}"
        upload_url = init.headers.get('Location')
        with open(video_path, 'rb') as f:
            up = requests.put(upload_url, headers={'Content-Type': 'video/mp4'},
                              data=f, timeout=600)
        if up.status_code in [200, 201]:
            return up.json().get('id'), None
        return None, up.text
    except Exception as e:
        return None, str(e)

# ── Routes ────────────────────────────────────────────────────────────────────
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
            vid_id = get_video_id(url)
            if not vid_id:
                jobs[job_id].update({'status': 'error', 'message': 'Invalid YouTube URL!'})
                return

            # Get stream URL
            jobs[job_id].update({'progress': 10, 'message': 'Piped API থেকে stream নিচ্ছি...'})
            stream_url, audio_url, instance = get_stream_url(vid_id)
            if not stream_url:
                jobs[job_id].update({'status': 'error', 'message': 'Stream URL পাওয়া যায়নি! Piped API down থাকতে পারে।'})
                return

            jobs[job_id]['message'] = f'Stream পেয়েছি! ({instance})'

            # Download only first part for transcription (5 min max)
            jobs[job_id].update({'progress': 20, 'message': 'Transcription এর জন্য audio নামাচ্ছি...'})
            temp_video = os.path.join(TEMP_DIR, f"{job_id}_full.mp4")
            
            # Download with time limit for transcription
            dl_cmd = ['ffmpeg', '-i', stream_url, '-t', '3600',
                      '-c', 'copy', '-y', temp_video]
            subprocess.Popen(dl_cmd)
            time.sleep(15)  # Wait 15s to get some content

            # Transcribe
            jobs[job_id]['progress'] = 40
            transcript = None
            if cfg.get('groq_api_key') and os.path.exists(temp_video):
                transcript = transcribe(temp_video, cfg['groq_api_key'], job_id)

            # Analyze
            jobs[job_id]['progress'] = 60
            segments = analyze_viral(transcript, num_clips, cfg.get('gemini_api_key', ''), job_id)

            # Crop each segment directly from stream
            clips = []
            for i, seg in enumerate(segments[:num_clips]):
                jobs[job_id]['message'] = f"Clip {i+1}/{num_clips} crop করছি..."
                jobs[job_id]['progress'] = 60 + int((i / num_clips) * 35)
                
                clip_name = f"{job_id}_clip{i+1}"
                
                # Download just this segment from stream
                seg_path = os.path.join(TEMP_DIR, f"{clip_name}_raw.mp4")
                dl = subprocess.run([
                    'ffmpeg', '-ss', str(seg['start_time']),
                    '-to', str(seg['end_time']),
                    '-i', stream_url,
                    '-c', 'copy', '-y', seg_path
                ], capture_output=True, timeout=120)

                if os.path.exists(seg_path) and os.path.getsize(seg_path) > 1000:
                    clip_path = crop_shorts(seg_path, 0, seg['end_time'] - seg['start_time'], clip_name)
                    if clip_path:
                        seg['clip_name'] = clip_name
                        seg['preview_url'] = f'/outputs/{clip_name}.mp4'
                        clips.append(seg)

            jobs[job_id].update({
                'status': 'done', 'progress': 100,
                'message': f'✅ {len(clips)}টা clip তৈরি!',
                'clips': clips
            })
        except Exception as e:
            jobs[job_id].update({'status': 'error', 'message': str(e)})

    threading.Thread(target=run, daemon=True).start()
    return jsonify({'job_id': job_id})

@app.route('/api/manual-crop', methods=['POST'])
def api_manual_crop():
    data = request.json
    cfg = load_config()
    vid_id = get_video_id(data.get('url', ''))
    if not vid_id:
        return jsonify({'error': 'Invalid URL'}), 400

    stream_url, _, _ = get_stream_url(vid_id)
    if not stream_url:
        return jsonify({'error': 'Stream URL পাওয়া যায়নি'}), 400

    start = float(data.get('start', 0))
    end = float(data.get('end', 60))
    text = data.get('text', '')
    clip_name = f"manual_{uuid.uuid4().hex[:8]}"

    def run():
        seg_path = os.path.join(TEMP_DIR, f"{clip_name}_raw.mp4")
        subprocess.run(['ffmpeg', '-ss', str(start), '-to', str(end),
                        '-i', stream_url, '-c', 'copy', '-y', seg_path],
                       capture_output=True, timeout=120)
        if os.path.exists(seg_path):
            crop_shorts(seg_path, 0, end - start, clip_name, text if text else None)

    threading.Thread(target=run, daemon=True).start()
    return jsonify({'clip_name': clip_name, 'preview_url': f'/outputs/{clip_name}.mp4'})

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
    job_id = uuid.uuid4().hex[:8]
    jobs[job_id] = {'status': 'running', 'message': 'Upload হচ্ছে...', 'progress': 50}

    def run():
        vid_id, err = youtube_upload(
            video_path, data.get('title', 'Amazing Short'),
            data.get('description', ''), data.get('tags', []), access_token
        )
        if vid_id:
            jobs[job_id].update({'status': 'done', 'progress': 100,
                                 'message': f'✅ Uploaded! youtube.com/shorts/{vid_id}',
                                 'video_id': vid_id})
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

    prompt = f"""Create viral YouTube Shorts metadata:
Hook: {hook}
Why viral: {why}

Return ONLY JSON (no markdown):
{{"title": "catchy title emoji max 90 chars", "description": "engaging 200 chars + hashtags", "tags": ["#tag1","#tag2","#tag3","#tag4","#tag5"]}}"""

    try:
        r = requests.post(
            f'https://generativelanguage.googleapis.com/v1beta/models/gemini-1.5-flash:generateContent?key={gemini_key}',
            json={'contents': [{'parts': [{'text': prompt}]}]},
            timeout=30
        )
        if r.status_code == 200:
            text = r.json()['candidates'][0]['content']['parts'][0]['text']
            text = re.sub(r'```json|```', '', text).strip()
            return jsonify(json.loads(text))
    except:
        pass
    return jsonify({'title': f'🔥 {hook}', 'description': why,
                    'tags': ['#shorts', '#viral', '#trending']})

@app.route('/api/yt-auth-url')
def yt_auth_url():
    cfg = load_config()
    client_id = cfg.get('yt_client_id', '')
    if not client_id:
        return jsonify({'error': 'YouTube Client ID নেই'}), 400
    scope = 'https://www.googleapis.com/auth/youtube.upload'
    redirect_uri = request.host_url.rstrip('/') + '/api/yt-callback'
    url = (f"https://accounts.google.com/o/oauth2/v2/auth"
           f"?client_id={client_id}&redirect_uri={redirect_uri}"
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
        'redirect_uri': redirect_uri, 'grant_type': 'authorization_code'
    })
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
    port = int(os.environ.get('PORT', 8080))
    app.run(host='0.0.0.0', port=port, debug=False)
