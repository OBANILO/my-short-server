from flask import Flask, request, jsonify, send_from_directory
import subprocess
import os
import uuid
import requests
import threading
import time
import re
import json

app = Flask(__name__)

UPLOAD_FOLDER = '/tmp/short_jobs'
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
AUDIO_SEGMENTS_FOLDER = '/tmp/audio_segments'
os.makedirs(AUDIO_SEGMENTS_FOLDER, exist_ok=True)
JOBS_STATE_FILE = '/tmp/jobs_state.json'

# ─── Job Persistence (file-based, survives restarts) ─────────────────────────

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
    jobs = load_jobs()
    return jobs.get(job_id)

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
        if os.path.exists(path):
            return path
    return '/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf'

def get_italic_font():
    for path in [
        '/usr/share/fonts/truetype/freefont/FreeSerifBoldItalic.ttf',
        '/usr/share/fonts/truetype/dejavu/DejaVuSerif-BoldItalic.ttf',
        '/usr/share/fonts/truetype/ubuntu/Ubuntu-BI.ttf',
        '/usr/share/fonts/truetype/dejavu/DejaVuSans-BoldOblique.ttf',
    ]:
        if os.path.exists(path):
            return path
    return get_best_font()

# ─── Download Helpers ─────────────────────────────────────────────────────────

def download_file(url, dest_path):
    try:
        r = requests.get(url, timeout=180, stream=True)
        r.raise_for_status()
        with open(dest_path, 'wb') as f:
            for chunk in r.iter_content(chunk_size=8192):
                f.write(chunk)
        return dest_path
    except Exception as e:
        raise Exception(f"Download failed for {url}: {e}")

def download_pexels_video(pexels_url, dest_path, pexels_api_key=""):
    # Already a direct CDN video link
    if '.mp4' in pexels_url.lower() or 'videos/download' in pexels_url:
        return download_file(pexels_url, dest_path)

    # Not a pexels.com page — try downloading directly
    if 'pexels.com' not in pexels_url:
        return download_file(pexels_url, dest_path)

    # Extract video ID from page URL
    match = re.search(r'/video/[^/]+-(\d+)/?', pexels_url)
    if not match:
        match = re.search(r'(\d{5,})/?$', pexels_url)
    if not match:
        return download_file(pexels_url, dest_path)

    video_id = match.group(1)

    # Use Pexels API
    api_key_to_use = pexels_api_key or 'xC87vhy3Cf152ByhxRtakfR4mM2rRHN2NxGIlVqzUHQQ5VlB5ebYoCva'
    try:
        api_resp = requests.get(
            f"https://api.pexels.com/videos/videos/{video_id}",
            headers={"Authorization": api_key_to_use},
            timeout=30
        )
        if api_resp.status_code == 200:
            data  = api_resp.json()
            files = data.get('video_files', [])
            selected = None
            max_h    = 0
            for f in files:
                h = f.get('height', 0)
                if h <= 720 and h > max_h:
                    max_h    = h
                    selected = f['link']
            if not selected:
                for f in files:
                    if f.get('quality') == 'sd':
                        selected = f['link']
                        break
            if not selected and files:
                selected = files[0]['link']
            if selected:
                print(f"[Pexels] Downloading: {selected[:80]}")
                return download_file(selected, dest_path)
    except Exception as e:
        print(f"[Pexels API] Error: {e}")

    fallback_url = f"https://www.pexels.com/video/{video_id}/download/"
    return download_file(fallback_url, dest_path)

# ─── Video / Audio Info ───────────────────────────────────────────────────────

def get_video_info(video_path):
    """Returns (duration, width, height) — safe against N/A values."""
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
            if not v or v == 'N/A':
                continue
            if k == 'width':
                try: width = int(v)
                except: pass
            if k == 'height':
                try: height = int(v)
                except: pass
            if k == 'duration' and duration is None:
                try: duration = float(v)
                except: pass

    # Second attempt
    if duration is None:
        result2 = subprocess.run(
            ['ffprobe', '-v', 'error',
             '-show_entries', 'format=duration',
             '-of', 'default=noprint_wrappers=1:nokey=1',
             video_path],
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
        ['ffprobe', '-v', 'error',
         '-show_entries', 'format=duration',
         '-of', 'default=noprint_wrappers=1:nokey=1',
         audio_path],
        capture_output=True, text=True
    )
    v = result.stdout.strip()
    if v and v != 'N/A':
        try: return float(v)
        except: pass
    return 60.0

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
    name    = ffmpeg_escape(artist_name.upper())
    padding = 28
    alpha   = "0.875+0.125*sin(6.2832/4.0*t)"
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

# ─── Core: Build Short Video ──────────────────────────────────────────────────

def build_short_video(video_path, audio_path, output_path,
                      short_duration=60, artist_name="SORLUNE"):
    vid_duration, vid_w, vid_h = get_video_info(video_path)
    aud_duration = get_audio_duration(audio_path)

    print(f"[Build] vid={vid_duration}s aud={aud_duration}s cap={short_duration}s")

    final_duration = min(vid_duration, aud_duration, float(short_duration))
    if final_duration <= 0:
        final_duration = min(aud_duration, float(short_duration))

    font        = get_best_font()
    font_italic = get_italic_font()
    fade_out_st = max(final_duration - 2.0, final_duration * 0.90)

    scale_crop = (
        "scale=iw*max(1080/iw\\,1920/ih):ih*max(1080/iw\\,1920/ih),"
        "crop=1080:1920"
    )
    grade = (
        "eq=brightness=0.02:contrast=1.05:saturation=1.10,"
        "curves=r='0/0 0.5/0.52 1/1':g='0/0 0.5/0.49 1/0.96':b='0/0 0.5/0.44 1/0.88',"
        "vignette=PI/5"
    )
    fade      = f"fade=t=in:st=0:d=1.5,fade=t=out:st={fade_out_st:.2f}:d=2"
    pix       = "format=yuv420p"
    watermark = build_artist_watermark(font_italic, artist_name)
    vf        = ",".join([scale_crop, grade, fade, pix, watermark])

    cmd = [
        'ffmpeg', '-y',
        '-i', video_path,
        '-i', audio_path,
        '-vf', vf,
        '-map', '0:v:0',
        '-map', '1:a:0',
        '-c:v', 'libx264',
        '-preset', 'ultrafast',  # faster = less memory
        '-crf', '28',            # slightly lower quality = less memory
        '-c:a', 'aac',
        '-b:a', '128k',
        '-pix_fmt', 'yuv420p',
        '-t', str(final_duration),
        '-shortest',
        '-threads', '1',         # limit threads to save memory
        output_path
    ]
    return cmd, final_duration

def generate_short_job(job_id, video_path, audio_path, output_path,
                       short_duration=60, artist_name="SORLUNE"):
    try:
        save_job(job_id, {'status': 'processing', 'video_url': None})

        cmd, final_duration = build_short_video(
            video_path, audio_path, output_path,
            short_duration=short_duration,
            artist_name=artist_name
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
    short_duration = int(data.get('duration', 60))

    if not pexels_url or not audio_url:
        return jsonify({'error': 'Missing pexels_url or audio_url'}), 400

    print(f"[generate-short] key={api_key}")

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
                if os.path.exists(f):
                    os.remove(f)

            save_job(job_id, {'status': 'downloading_video'})
            print(f"[Job {job_id}] Downloading video...")
            download_pexels_video(pexels_url, video_path, pexels_api_key)
            print(f"[Job {job_id}] Video: {os.path.getsize(video_path)} bytes")

            save_job(job_id, {'status': 'downloading_audio'})
            print(f"[Job {job_id}] Downloading audio...")
            download_file(audio_url, audio_path)
            print(f"[Job {job_id}] Audio: {os.path.getsize(audio_path)} bytes")

            generate_short_job(
                job_id, video_path, audio_path, output_path,
                short_duration=short_duration,
                artist_name=artist_name
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
    segment_duration = int(data.get('segment_duration', 60))
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
    start = 0
    idx   = 0
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
        start += segment_duration
        idx   += 1

    os.remove(audio_path)
    return jsonify({'segments': segments}), 200


@app.route('/audio_segments/<filename>', methods=['GET'])
def serve_audio_segment(filename):
    return send_from_directory(AUDIO_SEGMENTS_FOLDER, filename)


@app.route('/health', methods=['GET'])
def health():
    return jsonify({'status': 'ok', 'message': 'My Short server running'}), 200


if __name__ == '__main__':
    app.run(
        host='0.0.0.0',
        port=int(os.environ.get('PORT', 8080)),
        debug=False
    )
