import atexit
import datetime
import gzip
import os
import re
import sys
from pathlib import Path
from urllib.parse import urljoin, quote, urlparse, unquote

import requests
import xmltv
from apscheduler.schedulers.background import BackgroundScheduler
from flask import Flask, redirect, render_template, Response, stream_with_context, request
from tqdm import tqdm

from magiogo import *
from parse_season_number import parse_season_number

app = Flask(__name__, static_url_path="/", static_folder="public")
Path("public").mkdir(exist_ok=True)
last_refresh = None

UA = 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:137.0) Gecko/20100101 Firefox/137.0'
PROXY_HEADERS = {
    'User-Agent': UA,
    'Origin': 'https://www.magiogo.sk',
    'Referer': 'https://www.magiogo.sk/',
    'Accept': '*/*',
    'Accept-Language': 'sk-SK,sk;q=0.9',
    'Accept-Encoding': 'identity',
    'Connection': 'keep-alive',
    'Sec-Fetch-Dest': 'empty',
    'Sec-Fetch-Mode': 'cors',
    'Sec-Fetch-Site': 'cross-site',
}


def server_url():
    return os.environ.get('MAGIO_SERVER_PUBLIC_URL', 'http://127.0.0.1:5000')


def cdn_base_dir(url):
    """Vráti base dir CDN URL (cesta po posledné /)."""
    parsed = urlparse(url)
    path = parsed.path
    if '/Manifest' in path:
        base_path = path[:path.index('/Manifest') + 1]
    elif path.endswith('/'):
        base_path = path
    else:
        base_path = path.rsplit('/', 1)[0] + '/'
    return parsed.scheme + "://" + parsed.netloc + base_path


def make_base_url(cdn_url):
    """
    Vytvorí BaseURL pre MPD tak aby VLC mohol priamo pripojiť relatívnu cestu.
    Format: https://render.com/cdn/PATH?h=HOST
    VLC zostaví: BaseURL + "S!d2Ea.../Fragments(video=Init)"
    = https://render.com/cdn/PATH/S!d2Ea.../Fragments(video=Init)?h=HOST
    """
    parsed = urlparse(cdn_url)
    path = parsed.path
    if '/Manifest' in path:
        base_path = path[:path.index('/Manifest') + 1]
    elif path.endswith('/'):
        base_path = path
    else:
        base_path = path.rsplit('/', 1)[0] + '/'

    return f"{server_url()}/cdn/{parsed.netloc}{base_path}?h={parsed.netloc}"


def rewrite_mpd(content, base_url):
    """Vloží <BaseURL> do MPD – VLC sám zostaví správne URL s expandovanými premennými."""
    base_url_element = f'<BaseURL>{make_base_url(base_url)}</BaseURL>'
    if '<Period' in content:
        return re.sub(r'(<Period[^>]*>)', r'\1' + base_url_element, content, count=1)
    return re.sub(r'(<MPD[^>]*>)', r'\1' + base_url_element, content, count=1)


def rewrite_m3u8(content, base_url):
    """Prepíše URL v M3U8 playlistoch cez /proxy."""
    parsed = urlparse(base_url)
    base_dir = parsed.scheme + "://" + parsed.netloc + "/".join(parsed.path.split("/")[:-1]) + "/"
    lines = content.splitlines()
    new_lines = []
    for line in lines:
        stripped = line.strip()
        if stripped and not stripped.startswith('#'):
            abs_url = stripped if stripped.startswith('http') else urljoin(base_dir, stripped)
            new_lines.append(f"{server_url()}/proxy?url={quote(abs_url, safe='')}")
        else:
            new_lines.append(line)
    return '\n'.join(new_lines)


def proxy_stream(url):
    # Debug log
    print(f"PROXY: {url}", flush=True)
    try:
        upstream = requests.get(
            url,
            headers=PROXY_HEADERS,
            stream=True,
            timeout=30,
            allow_redirects=True,
            verify=True,
        )
    except requests.exceptions.ConnectionError as e:
        print(f"PROXY ConnectionError: {e} | URL: {url}", flush=True)
        return Response(f'Connection error: {e}', status=502)
    except Exception as e:
        print(f"PROXY Error: {e} | URL: {url}", flush=True)
        return Response(f'Proxy error: {e}', status=502)

    print(f"PROXY status={upstream.status_code} ct={upstream.headers.get('Content-Type')} | {url[:100]}", flush=True)

    if upstream.status_code != 200:
        return Response(f'Upstream {upstream.status_code}: {url}', status=upstream.status_code)

    content_type = upstream.headers.get('Content-Type', 'application/octet-stream')
    is_mpd  = '.mpd' in url.lower() or 'mpd' in content_type or 'Manifest' in url
    is_m3u8 = '.m3u8' in url.lower() or 'm3u' in content_type

    if is_mpd:
        rewritten = rewrite_mpd(upstream.text, url)
        return Response(rewritten, content_type='application/dash+xml; charset=utf-8',
                        headers={'Access-Control-Allow-Origin': '*'})

    if is_m3u8:
        rewritten = rewrite_m3u8(upstream.text, url)
        return Response(rewritten, content_type='application/x-mpegURL; charset=utf-8',
                        headers={'Access-Control-Allow-Origin': '*'})

    def generate():
        for chunk in upstream.iter_content(chunk_size=65536):
            if chunk:
                yield chunk

    return Response(stream_with_context(generate()), content_type=content_type,
                    headers={'Access-Control-Allow-Origin': '*', 'Cache-Control': 'no-cache'})


# ── Routes ────────────────────────────────────────────────────────────────────

@app.route('/')
def index():
    return render_template("index.html", last_refresh=last_refresh)


@app.route('/channel/<channel_id>')
def channel_proxy(channel_id):
    stream_info = magio.channel_stream_info(channel_id)
    return proxy_stream(stream_info.url)


@app.route('/proxy')
def proxy_route():
    url = request.args.get('url')
    if not url:
        return 'Missing url', 400
    return proxy_stream(url)


@app.route('/cdn/<path:cdn_path>')
def cdn_proxy(cdn_path):
    """
    Path-based proxy. VLC zostaví URL ako:
    /cdn/s1-h4.cdn.magio.tv/.../index.mpd/S!d2Ea.../Fragments(video=Init)?h=s1-h4.cdn.magio.tv
    """
    cdn_host = request.args.get('h')
    if not cdn_host:
        # Skús extrahovať host z prvého segmentu cesty
        parts = cdn_path.split('/', 1)
        cdn_host = parts[0]
        path_rest = parts[1] if len(parts) > 1 else ''
        cdn_path = f"{cdn_host}/{path_rest}"

    # Zostaví absolútnu CDN URL
    # cdn_path môže obsahovať host aj cestu: "s1-h4.cdn.magio.tv/___PARAM_.../Fragments(...)"
    if cdn_path.startswith(cdn_host):
        real_path = cdn_path[len(cdn_host):]
    else:
        real_path = '/' + cdn_path

    url = f"https://{cdn_host}{real_path}"

    # Pridaj query string okrem h=
    qs_parts = [(k, v) for k, v in request.args.items() if k != 'h']
    if qs_parts:
        url += '?' + '&'.join(f"{k}={v}" for k, v in qs_parts)

    return proxy_stream(url)


@app.errorhandler(404)
def page_not_found(e):
    return redirect('/')


# ── Generators ────────────────────────────────────────────────────────────────

def gzip_file(file_path):
    with open(file_path, 'rb') as src, gzip.open(f'{file_path}.gz', 'wb') as dst:
        dst.writelines(src)


def generate_m3u8(channels):
    pub_url = os.environ.get('MAGIO_SERVER_PUBLIC_URL', 'http://127.0.0.1:5000')
    with open("public/magioPlaylist.m3u8", "w", encoding="utf-8") as f:
        f.write("#EXTM3U\n")
        for ch in tqdm(channels, desc="Generating .m3u8", unit="ch", file=sys.stdout):
            f.write(f'#EXTINF:-1 tvg-id="{ch.id}" tvg-logo="{ch.logo}",{ch.name}\n')
            f.write(f"{pub_url}/channel/{ch.id}\n")


def generate_xmltv(channels):
    date_from = datetime.datetime.now()
    date_to   = datetime.datetime.now() + datetime.timedelta(days=int(os.environ.get('MAGIO_GUIDE_DAYS', 7)))
    channel_ids = [c.id for c in channels]
    with tqdm(total=100, desc="Generating XMLTV", unit="pct", file=sys.stdout) as bar:
        last = {'v': 0}
        def prog(p):
            p = max(0, min(100, int(p)))
            if p > last['v']:
                bar.update(p - last['v'])
                last['v'] = p
        epg = magio.epg(channel_ids, date_from, date_to, progress=prog)
        prog(100)
    with open("public/magioGuide.xmltv", "wb") as gf:
        writer = xmltv.Writer(
            date=datetime.datetime.now().strftime("%Y%m%d%H%M%S"),
            generator_info_name="MagioGoIPTVServer",
            generator_info_url="",
            source_info_name="Magio GO Guide",
            source_info_url="https://skgo.magio.tv/v2/television/epg")
        for ch in channels:
            writer.addChannel({'display-name': [(ch.name, 'sk')], 'icon': [{'src': ch.logo}], 'id': ch.id})
        for (cid, programmes) in epg.items():
            for p in programmes:
                pd = {
                    'category': [(g, 'en') for g in p.genres],
                    'channel': cid,
                    'credits': {'producer': p.producers, 'actor': p.actors, 'writer': p.writers, 'director': p.directors},
                    'date': str(p.year),
                    'desc': [(p.description, '')],
                    'icon': [{'src': p.poster}, {'src': p.thumbnail}],
                    'length': {'units': 'seconds', 'length': str(p.duration)},
                    'start': p.start_time.strftime("%Y%m%d%H%M%S"),
                    'stop':  p.end_time.strftime("%Y%m%d%H%M%S"),
                    'title': [(p.title, '')],
                }
                if p.episodeNo is not None:
                    if p.seasonNo is None:
                        (p.title, p.seasonNo) = parse_season_number(p.title)
                        pd['title'] = [(p.title, '')]
                    pd['episode-num'] = [(f'{(p.seasonNo or 1)-1} . {(p.episodeNo or 1)-1} . 0', 'xmltv_ns')]
                writer.addProgramme(pd)
        writer.write(gf, True)
    gzip_file("public/magioGuide.xmltv")


def refresh():
    channels = magio.channels()
    generate_m3u8(channels)
    generate_xmltv(channels)
    print("Refresh done!")
    global last_refresh
    last_refresh = datetime.datetime.now().strftime("%d/%m/%Y %H:%M:%S")


# ── Startup ───────────────────────────────────────────────────────────────────

qualityString = os.environ.get('MAGIO_QUALITY', "HIGH")
quality = {"LOW": MagioQuality.low, "MEDIUM": MagioQuality.medium, "HIGH": MagioQuality.high, "EXTRA": MagioQuality.extra}[qualityString]
device_type = os.environ.get('MAGIO_DEVICE_TYPE', "OTT_STB")
device_name = os.environ.get('MAGIO_DEVICE_NAME', "Magio IPTV Server")
magio_username = os.environ.get('MAGIO_USERNAME', '1eabfvjdpp')
magio_password = os.environ.get('MAGIO_PASSWORD', 'Miriamka510')

print(f"Quality: {qualityString}, Device: {device_type}")
print("Logging in...")
magio = MagioGo("./storage", magio_username, magio_password, quality, device_type, device_name)
refresh()

scheduler = BackgroundScheduler(timezone="UTC")
scheduler.add_job(refresh, 'interval', hours=int(os.environ.get('MAGIO_GUIDE_REFRESH_HOURS', 12)))
scheduler.start()
atexit.register(lambda: scheduler.shutdown())
