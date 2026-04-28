from flask import Flask, request, jsonify, send_from_directory
import subprocess
import os
import uuid
import requests
import threading
import time
import re

app = Flask(__name__)
jobs = {}
UPLOAD_FOLDER = '/tmp/short_jobs'
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

# ─── Font Helpers ────────────────────────────────────────────────────────────

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
    headers = {'Cache-Control': 'no-cache', 'Pragma': 'no-cache'}
    r = requests.get(
        f"{url}?nocache={int(time.time())}",
        timeout=180, stream=True, headers=headers
    )
    if r.status_code != 200:
        r = requests.get(url, timeout=180, stream=True)
    r.raise_for_status()
    with open(dest_path, 'wb') as f:
        for chunk in r.iter_content(chunk_size=8192):
            f.write(chunk)
    return dest_path

def download_pexels_video(pexels_url, dest_path, pexels_api_key=""):
    """
    Accepts either:
      - A direct Pexels video file URL  (https://...pexels.com/.../...mp4)
      - A Pexels page URL               (https://www.pexels.com/video/xxx-NNNNNN/)
    """
    # If it's already a direct video file
    if pexels_url.lower().endswith('.mp4') or 'videos/download' in pexels_url:
        return download_file(pexels_url, dest_path)

    # Extract video ID from page URL like /video/camel-...-34535416/
    match = re.search(r'/video/[^/]+-(\d+)/?', pexels_url)
    if not match:
        # Try plain numeric ID at end
        match = re.search(r'(\d{5,})/?$', pexels_url)
    if not match:
        raise ValueError(f"Cannot extract Pexels video ID from: {pexels_url}")

    video_id = match.group(1)

    # Use Pexels API if key provided
    if pexels_api_key:
        api_resp = requests.get(
            f"https://api.pexels.com/videos/videos/{video_id}",
            headers={"Authorization": pexels_api_key},
            timeout=30
        )
        if api_resp.status_code == 200:
            data = api_resp.json()
            # Pick the best quality (HD preferred, else highest)
            files = data.get('video_files', [])
            files_sorted = sorted(files, key=lambda x: x.get('width', 0), reverse=True)
            best = None
            for f in files_sorted:
                if f.get('quality') in ('hd', 'sd'):
                    best = f
                    break
            if not best and files_sorted:
                best = files_sorted[0]
            if best:
                return download_file(best['link'], dest_path)

    # Fallback: try constructing a common direct download URL pattern
    fallback_url = f"https://www.pexels.com/video/{video_id}/download/"
    return download_file(fallback_url, dest_path)

def get_video_info(video_path):
    """Returns (duration, width, height) of a video."""
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
            if k == 'width':   width    = int(v)
            if k == 'height':  height   = int(v)
            if k == 'duration' and duration is None:
                try: duration = float(v)
                except: pass
    return duration or 30.0, width or 1080, height or 1920

def get_audio_duration(audio_path):
    result = subprocess.run(
        ['ffprobe', '-v', 'error',
         '-show_entries', 'format=duration',
         '-of', 'default=noprint_wrappers=1:nokey=1',
         audio_path],
        capture_output=True, text=True
    )
    return float(result.stdout.strip())

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

def build_artist_watermark(font_italic, artist_name="MY SHORT"):
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
                      short_duration=60, artist_name="MY SHORT"):
    """
    Overlay audio song on Pexels video.
    - Crop/scale to 9:16 vertical (1080x1920) for Shorts
    - Trim to min(video_duration, audio_duration, short_duration)
    - Replace original audio with song
    - Add subtle grade + watermark
    """
    vid_duration, vid_w, vid_h = get_video_info(video_path)
    aud_duration = get_audio_duration(audio_path)

    # Final duration: cap at short_duration (default 60s), use shortest of video/audio
    final_duration = min(vid_duration, aud_duration, short_duration)

    font        = get_best_font()
    font_italic = get_italic_font()

    fade_out_st = max(final_duration - 2, final_duration * 0.90)

    # ── Video filter chain ──
    # 1. Scale + crop to 9:16 vertical (1080x1920)
    scale_crop = (
        "scale=iw*max(1080/iw\\,1920/ih):ih*max(1080/iw\\,1920/ih),"
        "crop=1080:1920"
    )
    # 2. Color grade (cinematic warm tone)
    grade = (
        "eq=brightness=0.02:contrast=1.05:saturation=1.10,"
        "curves=r='0/0 0.5/0.52 1/1':g='0/0 0.5/0.49 1/0.96':b='0/0 0.5/0.44 1/0.88',"
        "vignette=PI/5"
    )
    # 3. Fade in/out
    fade = f"fade=t=in:st=0:d=1.5,fade=t=out:st={fade_out_st:.2f}:d=2"
    # 4. Pixel format
    pix = "format=yuv420p"
    # 5. Watermark
    watermark = build_artist_watermark(font_italic, artist_name)

    vf = ",".join([scale_crop, grade, fade, pix, watermark])

    cmd = [
        'ffmpeg', '-y',
        '-i', video_path,          # input 0: pexels video
        '-i', audio_path,          # input 1: song
        '-vf', vf,
        '-map', '0:v:0',           # video from pexels
        '-map', '1:a:0',           # audio from song
        '-c:v', 'libx264',
        '-preset', 'fast',
        '-crf', '23',
        '-c:a', 'aac',
        '-b:a', '192k',
        '-pix_fmt', 'yuv420p',
        '-t', str(final_duration),
        '-shortest',
        output_path
    ]
    return cmd, final_duration

def generate_short_job(job_id, video_path, audio_path, output_path,
                       short_duration=60, artist_name="MY SHORT"):
    try:
        jobs[job_id]['status'] = 'processing'

        cmd, final_duration = build_short_video(
            video_path, audio_path, output_path,
            short_duration=short_duration,
            artist_name=artist_name
        )
        jobs[job_id]['duration'] = round(final_duration, 1)

        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=600)

        if proc.returncode == 0 and os.path.exists(output_path):
            jobs[job_id]['status']    = 'completed'
            jobs[job_id]['video_url'] = f"/videos/{job_id}/{job_id}.mp4"
        else:
            jobs[job_id]['status'] = 'error'
            jobs[job_id]['error']  = proc.stderr[-3000:]
            print(f"[FFmpeg ERROR]\n{proc.stderr[-3000:]}")

    except Exception as e:
        jobs[job_id]['status'] = 'error'
        jobs[job_id]['error']  = str(e)

# ─── Routes ───────────────────────────────────────────────────────────────────

@app.route('/generate-short', methods=['POST'])
def generate_short():
    """
    POST JSON:
    {
      "pexels_url":    "https://www.pexels.com/video/camel-...-34535416/",
      "audio_url":     "https://...song.mp3",
      "api_key":       "my_unique_job_key",
      "pexels_api_key":"YOUR_PEXELS_API_KEY",   // optional but recommended
      "artist":        "MY SHORT",               // optional watermark name
      "duration":      60                        // optional max seconds (default 60)
    }
    """
    data = request.get_json()
    if not data:
        return jsonify({'error': 'No JSON data'}), 400

    pexels_url     = data.get('pexels_url')
    audio_url      = data.get('audio_url')
    api_key        = data.get('api_key', str(uuid.uuid4())[:8])
    pexels_api_key = data.get('pexels_api_key', '').strip()
    artist_name    = data.get('artist', 'MY SHORT').strip()
    short_duration = int(data.get('duration', 60))

    if not pexels_url or not audio_url:
        return jsonify({'error': 'Missing pexels_url or audio_url'}), 400

    job_id     = api_key
    job_folder = os.path.join(UPLOAD_FOLDER, job_id)
    os.makedirs(job_folder, exist_ok=True)

    video_path  = os.path.join(job_folder, 'pexels_video.mp4')
    audio_path  = os.path.join(job_folder, 'audio.mp3')
    output_path = os.path.join(job_folder, f'{job_id}.mp4')

    jobs[job_id] = {'status': 'pending', 'video_url': None}

    def run():
        try:
            for f in [video_path, audio_path, output_path]:
                if os.path.exists(f):
                    os.remove(f)

            jobs[job_id]['status'] = 'downloading_video'
            download_pexels_video(pexels_url, video_path, pexels_api_key)

            jobs[job_id]['status'] = 'downloading_audio'
            download_file(audio_url, audio_path)

            generate_short_job(
                job_id, video_path, audio_path, output_path,
                short_duration=short_duration,
                artist_name=artist_name
            )
        except Exception as e:
            jobs[job_id]['status'] = 'error'
            jobs[job_id]['error']  = str(e)

    threading.Thread(target=run, daemon=True).start()
    return jsonify({'status': 'started', 'job_id': job_id}), 200


@app.route('/status/<api_key>', methods=['GET'])
def check_status(api_key):
    job = jobs.get(api_key)
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
        jobs.pop(api_key, None)
        import shutil
        job_folder = os.path.join(UPLOAD_FOLDER, api_key)
        if os.path.exists(job_folder):
            shutil.rmtree(job_folder, ignore_errors=True)
    return jsonify({'status': 'cleared'}), 200


@app.route('/health', methods=['GET'])
def health():
    return jsonify({'status': 'ok', 'message': 'My Short server running'}), 200


if __name__ == '__main__':
    app.run(
        host='0.0.0.0',
        port=int(os.environ.get('PORT', 8080)),
        debug=False
    )
