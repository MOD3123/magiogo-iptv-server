import atexit
import datetime
import gzip
import os
import re
import sys
from pathlib import Path
from urllib.parse import urljoin, quote, urlparse

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
}


def server_url():
    return os.environ.get('MAGIO_SERVER_PUBLIC_URL', 'http://127.0.0.1:5000')


def make_proxy_url(abs_url):
    return f"{server_url()}/proxy?url={quote(abs_url, safe='')}"


def rewrite_mpd(content, base_url):
    """
    Vloží <BaseURL> element do MPD aby všetky relatívne segmenty
    šli cez náš /proxy endpoint. NEmení media= a initialization= atribúty
    s template premennými ($Bandwidth$, $Time$, ...) – tie VLC expanduje sám.
    """
    # Vypočítaj base dir CDN (cesta po posledné /)
    parsed = urlparse(base_url)
    path = parsed.path
    # Magio MPD cesta končí na /Manifest alebo /index.mpd/Manifest
    if '/Manifest' in path:
        base_path = path[:path.index('/Manifest') + 1]
    elif path.endswith('/'):
        base_path = path
    else:
        base_path = path.rsplit('/', 1)[0] + '/'

    cdn_base = parsed.scheme + "://" + parsed.netloc + base_path
    proxy_base = make_proxy_url(cdn_base)

    # Vlož <BaseURL> hneď za <Period> tag (alebo na začiatok ak nie je)
    base_url_element = f'<BaseURL>{proxy_base}</BaseURL>'

    # Vlož za prvý <Period> tag
    if '<Period' in content:
        content = re.sub(
            r'(<Period[^>]*>)',
            r'\1' + base_url_element,
            content,
            count=1
        )
    elif '<MPD' in content:
        # Fallback – vlož za MPD otvárajúci tag
        content = re.sub(
            r'(<MPD[^>]*>)',
            r'\1' + base_url_element,
            content,
            count=1
        )

    return content


def rewrite_m3u8(content, base_url):
    """Prepíše URL v M3U8 playlistoch cez /proxy."""
    parsed = urlparse(base_url)
    base_dir = parsed.scheme + "://" + parsed.netloc + "/".join(parsed.path.split("/")[:-1]) + "/"

    lines = content.splitlines()
    new_lines = []
    for line in lines:
        stripped = line.strip()
        if stripped and not stripped.startswith('#'):
            if stripped.startswith('http'):
                new_lines.append(make_proxy_url(stripped))
            else:
                new_lines.append(make_proxy_url(urljoin(base_dir, stripped)))
        else:
            new_lines.append(line)
    return '\n'.join(new_lines)


@app.route('/')
def index():
    return render_template("index.html", last_refresh=last_refresh)


@app.route('/channel/<channel_id>')
def channel_proxy(channel_id):
    stream_info = magio.channel_stream_info(channel_id)
    return proxy_url(stream_info.url)


@app.route('/proxy')
def proxy_route():
    url = request.args.get('url')
    if not url:
        return 'Missing url parameter', 400
    return proxy_url(url)


def proxy_url(url):
    try:
        upstream = requests.get(url, headers=PROXY_HEADERS, stream=True, timeout=30)
    except Exception as e:
        return Response(f'Proxy error: {e}', status=502)

    if upstream.status_code != 200:
        return Response(f'Upstream error: {upstream.status_code} {url}', status=upstream.status_code)

    content_type = upstream.headers.get('Content-Type', 'application/octet-stream')
    is_mpd  = '.mpd' in url.lower() or 'mpd' in content_type
    is_m3u8 = '.m3u8' in url.lower() or 'm3u' in content_type

    if is_mpd:
        content = upstream.text
        rewritten = rewrite_mpd(content, url)
        return Response(rewritten, content_type='application/dash+xml; charset=utf-8',
                        headers={'Access-Control-Allow-Origin': '*'})

    if is_m3u8:
        content = upstream.text
        rewritten = rewrite_m3u8(content, url)
        return Response(rewritten, content_type='application/x-mpegURL; charset=utf-8',
                        headers={'Access-Control-Allow-Origin': '*'})

    # Binárne dáta (video/audio fragmenty) – stream priamo
    def generate():
        for chunk in upstream.iter_content(chunk_size=65536):
            if chunk:
                yield chunk

    return Response(
        stream_with_context(generate()),
        content_type=content_type,
        headers={'Access-Control-Allow-Origin': '*', 'Cache-Control': 'no-cache'}
    )


@app.errorhandler(404)
def page_not_found(e):
    return redirect('/')


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


# Startup
qualityString = os.environ.get('MAGIO_QUALITY', "HIGH")
quality = {"LOW": MagioQuality.low, "MEDIUM": MagioQuality.medium, "HIGH": MagioQuality.high, "EXTRA": MagioQuality.extra}[qualityString]
device_type = os.environ.get('MAGIO_DEVICE_TYPE', "OTT_STB")
device_name = os.environ.get('MAGIO_DEVICE_NAME', "Magio IPTV Server")
magio_username = os.environ.get('MAGIO_USERNAME', '1eabfvjdpp')
magio_password = os.environ.get('MAGIO_PASSWORD', 'Miriamka510')

print(f"Quality: {qualityString}, Device: {device_type}, Name: {device_name}")
print("Logging in...")
magio = MagioGo("./storage", magio_username, magio_password, quality, device_type, device_name)
refresh()

scheduler = BackgroundScheduler(timezone="UTC")
scheduler.add_job(refresh, 'interval', hours=int(os.environ.get('MAGIO_GUIDE_REFRESH_HOURS', 12)))
scheduler.start()
atexit.register(lambda: scheduler.shutdown())
