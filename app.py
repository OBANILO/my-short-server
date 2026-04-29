from flask import Flask, request, jsonify, send_from_directory
import subprocess
import os
import uuid
import requests
import threading
import time
import re
import json
import math

app = Flask(__name__)

UPLOAD_FOLDER = '/tmp/short_jobs'
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
AUDIO_SEGMENTS_FOLDER = '/tmp/audio_segments'
os.makedirs(AUDIO_SEGMENTS_FOLDER, exist_ok=True)
JOBS_STATE_FILE = '/tmp/jobs_state.json'

LYRICS_Y    = 0.75
EQ_CENTER_Y = 0.90
DARK_START  = 0.70

# ─── Job Persistence ──────────────────────────────────────────────────────────

def load_jobs():
    try:
        if os.path.exists(JOBS_STATE_FILE):
            with open(JOBS_STATE_FILE, 'r') as f:
                return json.load(f)
    except:
        pass
    return {}

def save_job(job_id, data):
    jobs = load_jobs()
    jobs[job_id] = data
    try:
        with open(JOBS_STATE_FILE, 'w') as f:
            json.dump(jobs, f)
    except:
        pass

def get_job(job_id):
    return load_jobs().get(job_id)

def delete_job(job_id):
    jobs = load_jobs()
    jobs.pop(job_id, None)
    try:
        with open(JOBS_STATE_FILE, 'w') as f:
            json.dump(jobs, f)
    except:
        pass

# ─── Font Helpers ─────────────────────────────────────────────────────────────

def get_best_font():
    for path in [
        '/usr/share/fonts/truetype/ubuntu/Ubuntu-B.ttf',
        '/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf',
        '/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf',
    ]:
        if os.path.exists(path): return path
    return '/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf'

def get_lyrics_font():
    for path in [
        '/usr/share/fonts/truetype/freefont/FreeSerifBold.ttf',
        '/usr/share/fonts/truetype/dejavu/DejaVuSerif-Bold.ttf',
        '/usr/share/fonts/truetype/liberation/LiberationSerif-Bold.ttf',
    ]:
        if os.path.exists(path): return path
    return get_best_font()

def get_italic_font():
    for path in [
        '/usr/share/fonts/truetype/freefont/FreeSerifBoldItalic.ttf',
        '/usr/share/fonts/truetype/dejavu/DejaVuSerif-BoldItalic.ttf',
        '/usr/share/fonts/truetype/ubuntu/Ubuntu-BI.ttf',
        '/usr/share/fonts/truetype/dejavu/DejaVuSans-BoldOblique.ttf',
    ]:
        if os.path.exists(path): return path
    return get_best_font()

# ─── Lyrics Helpers ───────────────────────────────────────────────────────────

_SECTION_WORDS = r'verse|chorus|bridge|hook|outro|intro|pre[\-\s]?chorus|post[\-\s]?chorus|refrain|interlude|instrumental|spoken|rap|breakdown|solo|ad[\-\s]?lib|vamp|coda|tag|skit|fade'
SECTION_REGEX = [re.compile(p, re.IGNORECASE) for p in [
    r'^\[.*\]$', r'^\(.*\)$',
    rf'^({_SECTION_WORDS})\s*[\d:.\-]*\s*$',
    rf'^({_SECTION_WORDS})\s*\d*\s*:$',
    r'^[\d\s\.\)\(\:\-]+$'
]]

def is_section_label(line):
    s = line.strip()
    return any(p.match(s) or p.match(s.rstrip(':').strip()) for p in SECTION_REGEX)

def split_lyrics_lines(text):
    if not text: return []
    return [l.strip() for l in text.replace('\r\n','\n').replace('\r','\n').split('\n')
            if l.strip() and not is_section_label(l.strip())]

def normalize_word(w):
    return re.sub(r"[^\w']", "", (w or "").lower()).strip()

def transcribe_audio_words_with_whisper(audio_path, openai_api_key):
    if not openai_api_key or not os.path.exists(audio_path): return []
    try:
        with open(audio_path, "rb") as audio_file:
            response = requests.post(
                "https://api.openai.com/v1/audio/transcriptions",
                headers={"Authorization": f"Bearer {openai_api_key}"},
                files={"file": audio_file},
                data={"model": "whisper-1", "response_format": "verbose_json",
                      "timestamp_granularities[]": "word"},
                timeout=300
            )
        if response.status_code != 200: return []
        data = response.json()
        cleaned = []
        for w in data.get("words", []):
            word_text = (w.get("word") or "").strip()
            start = w.get("start"); end = w.get("end")
            if not word_text or start is None or end is None: continue
            start, end = float(start), float(end)
            if end <= start: continue
            cleaned.append({"word": word_text, "norm": normalize_word(word_text), "start": start, "end": end})
        if cleaned: return cleaned
        seg_words = []
        for seg in data.get("segments", []):
            text = (seg.get("text") or "").strip()
            start = seg.get("start"); end = seg.get("end")
            if not text or start is None or end is None: continue
            seg_words.append({"word": text, "norm": normalize_word(text), "start": float(start), "end": float(end)})
        return seg_words
    except Exception as e:
        print(f"[Whisper] Error: {e}"); return []

def build_lines_from_words(words, max_gap=0.45, max_words=6, max_duration=3.0):
    if not words: return []
    lines = []; current = [words[0]]
    def flush(lw):
        if not lw: return None
        text = " ".join(w["word"] for w in lw).strip()
        return {"start": round(lw[0]["start"], 2), "end": round(lw[-1]["end"], 2), "text": text} if text else None
    for w in words[1:]:
        prev = current[-1]
        if (w["start"] - prev["end"] > max_gap or len(current) >= max_words
                or w["end"] - current[0]["start"] > max_duration):
            item = flush(current)
            if item: lines.append(item)
            current = [w]
        else:
            current.append(w)
    item = flush(current)
    if item: lines.append(item)
    cleaned = []
    for seg in lines:
        start = float(seg["start"]); end = float(seg["end"]); text = seg["text"].strip()
        if not text: continue
        min_dur = max(0.60, min(1.40, len(text.split()) * 0.22))
        if end - start < min_dur: end = start + min_dur
        if cleaned and start < cleaned[-1]["end"]:
            start = round(cleaned[-1]["end"] + 0.03, 2)
            end = max(end, start + min_dur)
        cleaned.append({"start": round(start, 2), "end": round(end, 2), "text": text})
    return cleaned

def transcribe_lyrics_with_whisper(audio_path, openai_api_key, lyrics_text=""):
    return build_lines_from_words(transcribe_audio_words_with_whisper(audio_path, openai_api_key))

# ─── FFmpeg Escape ────────────────────────────────────────────────────────────

def ffmpeg_escape(text):
    text = text.replace('\\', '\\\\')
    text = text.replace("'", "\u2019")
    text = text.replace(':', '\\:')
    text = text.replace('%', '\\%')
    text = text.replace('[', '\\[')
    text = text.replace(']', '\\]')
    text = text.replace(',', '\\,')
    return text

# ─── Watermark ────────────────────────────────────────────────────────────────

def build_artist_watermark(font_italic, artist_name="SORLUNE"):
    name = ffmpeg_escape(artist_name.upper())
    padding = 28
    alpha = "0.875+0.125*sin(6.2832/4.0*t)"
    watermark = (
        f"drawtext=fontfile={font_italic}:text='{name}':"
        f"fontsize=34:fontcolor=0xD4AF37@1.0:"
        f"borderw=2:bordercolor=black@0.80:"
        f"shadowcolor=black@0.70:shadowx=2:shadowy=2:"
        f"x=w-text_w-{padding}:y={padding}:alpha='{alpha}'"
    )
    underline = (
        f"drawtext=fontfile={font_italic}:text='\u2014\u2014\u2014\u2014\u2014\u2014\u2014':"
        f"fontsize=14:fontcolor=0xD4AF37@1.0:"
        f"x=w-text_w-{padding}:y={padding+42}:alpha='{alpha}'"
    )
    return ",".join([watermark, underline])

# ─── Karaoke Filter ───────────────────────────────────────────────────────────

def wrap_lyric_line(text, max_chars=32):
    if len(text) <= max_chars: return [text]
    words = text.split()
    best_split, best_diff = len(words) // 2, float('inf')
    for i in range(1, len(words)):
        p1, p2 = " ".join(words[:i]), " ".join(words[i:])
        diff = abs(len(p1) - len(p2))
        if diff < best_diff and len(p1) <= max_chars and len(p2) <= max_chars:
            best_diff, best_split = diff, i
    return [" ".join(words[:best_split]), " ".join(words[best_split:])]

def build_karaoke_filter(segments, font, lyrics_font=None):
    if lyrics_font is None: lyrics_font = font
    if not segments: return ""
    parts = []; FONT_SIZE = 42; LINE_HEIGHT = 52; MAX_CHARS = 32
    for seg in segments:
        start, end, raw_text = seg["start"], seg["end"], seg["text"]
        dur = max(end - start, 0.5)
        fade_dur = min(0.18, dur / 5)
        alpha_expr = (
            f"if(between(t,{start},{start+fade_dur}),(t-{start})/{fade_dur},"
            f"if(between(t,{start+fade_dur},{end-fade_dur}),1,"
            f"if(between(t,{end-fade_dur},{end}),({end}-t)/{fade_dur},0)))"
        )
        lines = wrap_lyric_line(raw_text, max_chars=MAX_CHARS)
        if len(lines) == 1:
            parts.append(
                f"drawtext=fontfile={lyrics_font}:text='{ffmpeg_escape(lines[0])}':"
                f"fontsize={FONT_SIZE}:fontcolor=white@1.0:"
                f"borderw=4:bordercolor=black@1.0:"
                f"shadowcolor=black@0.95:shadowx=3:shadowy=3:"
                f"x=(w-text_w)/2:y=h*{LYRICS_Y}:alpha='{alpha_expr}'"
            )
        else:
            base_y = LYRICS_Y - 0.05
            for li, line in enumerate(lines):
                parts.append(
                    f"drawtext=fontfile={lyrics_font}:text='{ffmpeg_escape(line)}':"
                    f"fontsize={FONT_SIZE}:fontcolor=white@1.0:"
                    f"borderw=4:bordercolor=black@1.0:"
                    f"shadowcolor=black@0.95:shadowx=3:shadowy=3:"
                    f"x=(w-text_w)/2:y=h*{base_y}+{li*LINE_HEIGHT}:alpha='{alpha_expr}'"
                )
    return ",".join(parts)

# ─── EQ Bar ───────────────────────────────────────────────────────────────────

def build_eq_bar(font):
    parts = []; bar_count = 24; bar_gap = 12; half = bar_count // 2
    center_y = f"h*{EQ_CENTER_Y}"
    freqs  = [1.3,2.1,2.7,1.9,3.1,2.4,1.7,2.9,2.2,3.5,2.0,2.8,2.1,2.8,2.0,3.5,2.2,2.9,1.7,2.4,3.1,1.9,2.7,2.1]
    phases = [0.0,0.5,1.1,1.7,0.3,0.9,1.5,0.2,0.8,1.4,0.6,1.2,0.0,1.2,0.6,1.4,0.8,0.2,1.5,0.9,0.3,1.7,1.1,0.5]
    for i in range(bar_count):
        dist = abs(i - half) / half
        amplitude = int(5 + 30 * math.exp(-2.5 * dist * dist))
        alpha_up  = 0.90 - 0.25 * dist
        alpha_dwn = 0.40 - 0.15 * dist
        offset = (i - half) * bar_gap
        bar_x  = f"(w/2+({offset})-tw/2)"
        fs_expr = f"4+{amplitude}*abs(sin(t*{freqs[i]}+{phases[i]}))"
        parts.append(f"drawtext=fontfile={font}:text='|':fontsize={fs_expr}:fontcolor=0xD4AF37@{alpha_up:.2f}:x={bar_x}:y=({center_y})-text_h")
        parts.append(f"drawtext=fontfile={font}:text='|':fontsize={fs_expr}:fontcolor=0xB8860B@{alpha_dwn:.2f}:x={bar_x}:y={center_y}")
    return ",".join(parts)

# ─── Dark Overlay ─────────────────────────────────────────────────────────────

def build_dark_overlay(font):
    return (
        f"drawtext=fontfile={font}:text=' ':fontsize=1:fontcolor=black@0:"
        f"box=1:boxcolor=black@0.50:boxborderw=0:"
        f"x=0:y=h*{DARK_START}:fix_bounds=1"
    )

# ─── Video / Audio Info ───────────────────────────────────────────────────────

def get_video_info(video_path):
    result = subprocess.run(
        ['ffprobe', '-v', 'error',
         '-select_streams', 'v:0',
         '-show_entries', 'stream=width,height,duration',
         '-show_entries', 'format=duration',
         '-of', 'default=noprint_wrappers=1',
         video_path],
        capture_output=True, text=True
    )
    width = height = duration = None
    for line in result.stdout.splitlines():
        if '=' in line:
            k, v = line.split('=', 1)
            v = v.strip()
            if not v or v == 'N/A': continue
            if k == 'width':
                try: width = int(v)
                except: pass
            if k == 'height':
                try: height = int(v)
                except: pass
            if k == 'duration' and duration is None:
                try: duration = float(v)
                except: pass
    if duration is None:
        result2 = subprocess.run(
            ['ffprobe', '-v', 'error', '-show_entries', 'format=duration',
             '-of', 'default=noprint_wrappers=1:nokey=1', video_path],
            capture_output=True, text=True
        )
        v2 = result2.stdout.strip()
        if v2 and v2 != 'N/A':
            try: duration = float(v2)
            except: pass
    print(f"[VideoInfo] duration={duration} width={width} height={height}")
    return duration or 30.0, width or 1080, height or 1920

def get_audio_duration(audio_path):
    result = subprocess.run(
        ['ffprobe', '-v', 'error', '-show_entries', 'format=duration',
         '-of', 'default=noprint_wrappers=1:nokey=1', audio_path],
        capture_output=True, text=True
    )
    v = result.stdout.strip()
    if v and v != 'N/A':
        try: return float(v)
        except: pass
    return 45.0

# ─── Core: Build Short Video ──────────────────────────────────────────────────

def build_short_video(video_path, audio_path, output_path,
                      short_duration=45, artist_name="SORLUNE",
                      lyrics_segments=None):

    aud_duration = get_audio_duration(audio_path)

    # Audio is master — video just loops as background
    final_duration = min(aud_duration, float(short_duration))
    print(f"[Build] aud={aud_duration}s final={final_duration}s")

    font        = get_best_font()
    font_italic = get_italic_font()
    lyrics_font = get_lyrics_font()

    fade_out_st = max(final_duration - 2.0, final_duration * 0.90)

    # Scale + crop to 9:16 vertical (1080x1920)
    scale_crop = (
        "scale=iw*max(1080/iw\\,1920/ih):ih*max(1080/iw\\,1920/ih),"
        "crop=1080:1920"
    )
    # Color grade
    grade = (
        "eq=brightness=0.02:contrast=1.05:saturation=1.10,"
        "curves=r='0/0 0.5/0.52 1/1':g='0/0 0.5/0.49 1/0.96':b='0/0 0.5/0.44 1/0.88',"
        "vignette=PI/5"
    )
    fade      = f"fade=t=in:st=0:d=1.5,fade=t=out:st={fade_out_st:.2f}:d=2"
    pix       = "format=yuv420p"
    dark      = build_dark_overlay(font)
    watermark = build_artist_watermark(font_italic, artist_name)
    eq        = build_eq_bar(font)

    vf_parts = [scale_crop, grade, fade, pix, dark, watermark]

    # Add karaoke if lyrics available
    if lyrics_segments:
        karaoke = build_karaoke_filter(lyrics_segments, font, lyrics_font)
        if karaoke:
            vf_parts.append(karaoke)

    vf_parts.append(eq)
    vf = ",".join(vf_parts)

    cmd = [
        'ffmpeg', '-y',
        '-stream_loop', '-1',    # ✅ loop video as background
        '-i', video_path,
        '-i', audio_path,
        '-vf', vf,
        '-map', '0:v:0',
        '-map', '1:a:0',
        '-c:v', 'libx264',
        '-preset', 'ultrafast',
        '-crf', '28',
        '-c:a', 'aac',
        '-b:a', '128k',
        '-pix_fmt', 'yuv420p',
        '-t', str(final_duration),  # ✅ duration = audio length
        '-shortest',
        '-threads', '1',
        output_path
    ]
    return cmd, final_duration

def generate_short_job(job_id, video_path, audio_path, output_path,
                       short_duration=45, artist_name="SORLUNE",
                       lyrics_segments=None):
    try:
        save_job(job_id, {'status': 'processing'})
        cmd, final_duration = build_short_video(
            video_path, audio_path, output_path,
            short_duration=short_duration,
            artist_name=artist_name,
            lyrics_segments=lyrics_segments
        )
        print(f"[FFmpeg] Starting job {job_id}...")
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
        if proc.returncode == 0 and os.path.exists(output_path):
            save_job(job_id, {
                'status': 'completed',
                'video_url': f"/videos/{job_id}/{job_id}.mp4",
                'duration': round(final_duration, 1)
            })
            print(f"[Job {job_id}] ✅ Done!")
        else:
            error_msg = proc.stderr[-2000:] if proc.stderr else 'Unknown error'
            save_job(job_id, {'status': 'error', 'error': error_msg})
            print(f"[FFmpeg ERROR] {error_msg}")
    except Exception as e:
        save_job(job_id, {'status': 'error', 'error': str(e)})
        print(f"[Job {job_id}] ❌ {e}")

# ─── Routes ───────────────────────────────────────────────────────────────────

@app.route('/generate-short', methods=['POST'])
def generate_short():
    data = request.get_json()
    if not data:
        return jsonify({'error': 'No JSON data'}), 400

    pexels_url     = data.get('pexels_url')
    audio_url      = data.get('audio_url')
    api_key        = data.get('api_key', str(uuid.uuid4())[:8])
    pexels_api_key = data.get('pexels_api_key', '').strip()
    artist_name    = data.get('artist', 'SORLUNE').strip()
    short_duration = int(data.get('duration', 45))
    lyrics_text    = data.get('lyrics', '').strip()
    openai_key     = data.get('openai_key', '').strip()

    if not pexels_url or not audio_url:
        return jsonify({'error': 'Missing pexels_url or audio_url'}), 400

    print(f"[generate-short] key={api_key} dur={short_duration}s")

    job_id     = api_key
    job_folder = os.path.join(UPLOAD_FOLDER, job_id)
    os.makedirs(job_folder, exist_ok=True)

    video_path  = os.path.join(job_folder, 'pexels_video.mp4')
    audio_path  = os.path.join(job_folder, 'audio.mp3')
    output_path = os.path.join(job_folder, f'{job_id}.mp4')

    save_job(job_id, {'status': 'pending', 'video_url': None})

    def run():
        try:
            for f in [video_path, audio_path, output_path]:
                if os.path.exists(f): os.remove(f)

            save_job(job_id, {'status': 'downloading_video'})
            download_pexels_video(pexels_url, video_path, pexels_api_key)
            print(f"[Job {job_id}] Video: {os.path.getsize(video_path)} bytes")

            save_job(job_id, {'status': 'downloading_audio'})
            download_file(audio_url, audio_path)
            print(f"[Job {job_id}] Audio: {os.path.getsize(audio_path)} bytes")

            # Lyrics via Whisper
            lyrics_segments = []
            if openai_key:
                try:
                    save_job(job_id, {'status': 'transcribing_lyrics'})
                    lyrics_segments = transcribe_lyrics_with_whisper(audio_path, openai_key, lyrics_text)
                    print(f"[Job {job_id}] Lyrics: {len(lyrics_segments)} segments")
                except Exception as e:
                    print(f"[Lyrics] Whisper failed: {e}")
                    lyrics_segments = []

            # Fallback: time-based lyrics
            if not lyrics_segments and lyrics_text:
                duration = get_audio_duration(audio_path)
                lines = split_lyrics_lines(lyrics_text)
                if lines:
                    step = max(duration / len(lines), 1.8)
                    current = 0.0
                    for line in lines:
                        lyrics_segments.append({
                            "start": round(current, 2),
                            "end": round(min(current + step, duration), 2),
                            "text": line
                        })
                        current += step

            generate_short_job(
                job_id, video_path, audio_path, output_path,
                short_duration=short_duration,
                artist_name=artist_name,
                lyrics_segments=lyrics_segments
            )
        except Exception as e:
            save_job(job_id, {'status': 'error', 'error': str(e)})
            print(f"[Job {job_id}] ❌ {e}")

    threading.Thread(target=run, daemon=True).start()
    return jsonify({'status': 'started', 'job_id': job_id}), 200


@app.route('/status/<api_key>', methods=['GET'])
def check_status(api_key):
    job = get_job(api_key)
    if not job:
        return jsonify({'status': 'not_found'}), 200
    response = {'status': job['status']}
    if job['status'] == 'completed':
        response['video_url'] = (
            request.host_url.rstrip('/') +
            f'/videos/{api_key}/{api_key}.mp4'
        )
        response['duration'] = job.get('duration')
    if job.get('error'):
        response['error'] = job['error']
    return jsonify(response), 200


@app.route('/videos/<job_id>/<filename>', methods=['GET'])
def serve_video(job_id, filename):
    return send_from_directory(os.path.join(UPLOAD_FOLDER, job_id), filename)


@app.route('/clear-cache', methods=['POST'])
def clear_cache():
    data    = request.get_json()
    api_key = data.get('api_key') if data else None
    if api_key:
        delete_job(api_key)
        import shutil
        job_folder = os.path.join(UPLOAD_FOLDER, api_key)
        if os.path.exists(job_folder):
            shutil.rmtree(job_folder, ignore_errors=True)
    return jsonify({'status': 'cleared'}), 200


@app.route('/process-audio', methods=['POST'])
def process_audio():
    data = request.get_json()
    if not data:
        return jsonify({'error': 'No JSON data'}), 400
    audio_url        = data.get('url')
    segment_duration = int(data.get('segment_duration', 45))
    if not audio_url:
        return jsonify({'error': 'Missing url'}), 400

    session_id = str(uuid.uuid4())[:8]
    audio_path = os.path.join(AUDIO_SEGMENTS_FOLDER, f'{session_id}_input.mp3')

    try:
        download_file(audio_url, audio_path)
    except Exception as e:
        return jsonify({'error': f'Download failed: {str(e)}'}), 500

    total_duration = get_audio_duration(audio_path)
    segments = []
    start = 0; idx = 0
    while start < total_duration:
        seg_fn   = f'{session_id}_seg{idx:03d}.mp3'
        seg_path = os.path.join(AUDIO_SEGMENTS_FOLDER, seg_fn)
        proc = subprocess.run(
            ['ffmpeg', '-y', '-i', audio_path,
             '-ss', str(start), '-t', str(segment_duration),
             '-c:a', 'libmp3lame', '-b:a', '128k', seg_path],
            capture_output=True, timeout=120
        )
        if proc.returncode == 0 and os.path.exists(seg_path):
            segments.append(seg_fn)
        start += segment_duration; idx += 1

    os.remove(audio_path)
    return jsonify({'segments': segments}), 200


@app.route('/audio_segments/<filename>', methods=['GET'])
def serve_audio_segment(filename):
    return send_from_directory(AUDIO_SEGMENTS_FOLDER, filename)


@app.route('/health', methods=['GET'])
def health():
    return jsonify({'status': 'ok', 'message': 'My Short server running'}), 200


def download_file(url, dest_path):
    try:
        r = requests.get(url, timeout=180, stream=True)
        r.raise_for_status()
        with open(dest_path, 'wb') as f:
            for chunk in r.iter_content(chunk_size=8192):
                f.write(chunk)
        return dest_path
    except Exception as e:
        raise Exception(f"Download failed: {e}")

def download_pexels_video(pexels_url, dest_path, pexels_api_key=""):
    if '.mp4' in pexels_url.lower() or 'videos/download' in pexels_url:
        return download_file(pexels_url, dest_path)
    if 'pexels.com' not in pexels_url:
        return download_file(pexels_url, dest_path)
    match = re.search(r'/video/[^/]+-(\d+)/?', pexels_url)
    if not match:
        match = re.search(r'(\d{5,})/?$', pexels_url)
    if not match:
        return download_file(pexels_url, dest_path)
    video_id = match.group(1)
    api_key_to_use = pexels_api_key or 'xC87vhy3Cf152ByhxRtakfR4mM2rRHN2NxGIlVqzUHQQ5VlB5ebYoCva'
    try:
        api_resp = requests.get(
            f"https://api.pexels.com/videos/videos/{video_id}",
            headers={"Authorization": api_key_to_use},
            timeout=30
        )
        if api_resp.status_code == 200:
            data = api_resp.json()
            files = data.get('video_files', [])
            selected = None; max_h = 0
            for f in files:
                h = f.get('height', 0)
                if h <= 720 and h > max_h:
                    max_h = h; selected = f['link']
            if not selected:
                for f in files:
                    if f.get('quality') == 'sd':
                        selected = f['link']; break
            if not selected and files:
                selected = files[0]['link']
            if selected:
                return download_file(selected, dest_path)
    except Exception as e:
        print(f"[Pexels API] Error: {e}")
    return download_file(f"https://www.pexels.com/video/{video_id}/download/", dest_path)


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 8080)), debug=False)
