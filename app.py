import atexit
import datetime
import gzip
import os
import sys
from pathlib import Path

import requests
import xmltv
from apscheduler.schedulers.background import BackgroundScheduler
from flask import Flask, redirect, render_template, Response, stream_with_context
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


@app.route('/')
def index():
    return render_template("index.html", last_refresh=last_refresh)


@app.route('/channel/<channel_id>')
def channel_proxy(channel_id):
    stream_info = magio.channel_stream_info(channel_id)
    url = stream_info.url
    return proxy_url(url)


@app.route('/proxy')
def proxy_redirect():
    from flask import request
    url = request.args.get('url')
    if not url:
        return 'Missing url', 400
    return proxy_url(url)


def proxy_url(url):
    """Proxy akéhokoľvek URL cez Render server – zachová IP tokeny."""
    is_mpd  = '.mpd'   in url.lower()
    is_m3u8 = '.m3u8'  in url.lower()

    upstream = requests.get(url, headers=PROXY_HEADERS, stream=True, timeout=30)

    if upstream.status_code != 200:
        return Response(f'Upstream error: {upstream.status_code}', status=upstream.status_code)

    content_type = upstream.headers.get('Content-Type', 'application/octet-stream')

    # Pre MPD/M3U8 manifesty – prepíš interné URL aby šli tiež cez proxy
    if is_mpd or is_m3u8 or 'mpd' in content_type or 'm3u' in content_type:
        content = upstream.text
        content = rewrite_manifest(content, url)
        return Response(content, content_type=content_type)

    # Pre video/audio fragmenty – stream priamo
    def generate():
        for chunk in upstream.iter_content(chunk_size=65536):
            if chunk:
                yield chunk

    return Response(
        stream_with_context(generate()),
        content_type=content_type,
        headers={
            'Cache-Control': 'no-cache',
            'Access-Control-Allow-Origin': '*',
        }
    )


def rewrite_manifest(content, base_url):
    """Prepíše absolútne aj relatívne URL v manifeste aby šli cez /proxy?url=..."""
    from urllib.parse import urljoin, quote
    import re

    server_url = os.environ.get('MAGIO_SERVER_PUBLIC_URL', 'http://127.0.0.1:5000')

    def make_proxy(url):
        if url.startswith('http'):
            abs_url = url
        else:
            abs_url = urljoin(base_url, url)
        return f"{server_url}/proxy?url={quote(abs_url, safe='')}"

    # MPD: src="..." a href="..."
    content = re.sub(
        r'(src|href)="(https?://[^"]+)"',
        lambda m: f'{m.group(1)}="{make_proxy(m.group(2))}"',
        content
    )

    # M3U8: riadky ktoré sú URL (nezačínajú #)
    lines = content.splitlines()
    new_lines = []
    for line in lines:
        stripped = line.strip()
        if stripped and not stripped.startswith('#'):
            if stripped.startswith('http'):
                new_lines.append(make_proxy(stripped))
            else:
                new_lines.append(make_proxy(stripped))
        else:
            new_lines.append(line)
    return '\n'.join(new_lines)


@app.errorhandler(404)
def page_not_found(e):
    return redirect('/')


def gzip_file(file_path):
    with open(file_path, 'rb') as src, gzip.open(f'{file_path}.gz', 'wb') as dst:
        dst.writelines(src)


def generate_m3u8(channels):
    magio_iptv_server_public_url = os.environ.get('MAGIO_SERVER_PUBLIC_URL', "http://127.0.0.1:5000")
    with open("public/magioPlaylist.m3u8", "w", encoding="utf-8") as text_file:
        text_file.write("#EXTM3U\n")
        for channel in tqdm(channels, total=len(channels), desc="Generating .m3u8 playlist", unit="ch", file=sys.stdout):
            text_file.write(f'#EXTINF:-1 tvg-id="{channel.id}" tvg-logo="{channel.logo}",{channel.name}\n')
            text_file.write(f"{magio_iptv_server_public_url}/channel/{channel.id}\n")


def generate_xmltv(channels):
    date_from = datetime.datetime.now() - datetime.timedelta(days=0)
    date_to = datetime.datetime.now() + datetime.timedelta(days=int(os.environ.get('MAGIO_GUIDE_DAYS', 7)))
    channel_ids = list(map(lambda c: c.id, channels))
    with tqdm(total=100, desc="Generating XMLTV guide", unit="pct", file=sys.stdout) as bar:
        last_progress = {'value': 0}

        def epg_progress(percent):
            percent = max(0, min(100, int(percent)))
            if percent <= last_progress['value']:
                return
            bar.update(percent - last_progress['value'])
            last_progress['value'] = percent

        epg = magio.epg(channel_ids, date_from, date_to, progress=epg_progress)
        epg_progress(100)

    with open("public/magioGuide.xmltv", "wb") as guide_file:
        writer = xmltv.Writer(
            date=datetime.datetime.now().strftime("%Y%m%d%H%M%S"),
            generator_info_name="MagioGoIPTVServer",
            generator_info_url="",
            source_info_name="Magio GO Guide",
            source_info_url="https://skgo.magio.tv/v2/television/epg")
        for channel in channels:
            channel_dict = {'display-name': [(channel.name, u'sk')],
                            'icon': [{'src': channel.logo}],
                            'id': channel.id}
            writer.addChannel(channel_dict)
        for (channel_id, programmes) in epg.items():
            for programme in programmes:
                programme_dict = {
                    'category': [(genre, u'en') for genre in programme.genres],
                    'channel': channel_id,
                    'credits': {'producer': [producer for producer in programme.producers],
                                'actor': [actor for actor in programme.actors],
                                'writer': [writer for writer in programme.writers],
                                'director': [director for director in programme.directors]},
                    'date': str(programme.year),
                    'desc': [(programme.description, u'')],
                    'icon': [{'src': programme.poster}, {'src': programme.thumbnail}],
                    'length': {'units': u'seconds', 'length': str(programme.duration)},
                    'start': programme.start_time.strftime("%Y%m%d%H%M%S"),
                    'stop': programme.end_time.strftime("%Y%m%d%H%M%S"),
                    'title': [(programme.title, u'')]}

                if programme.episodeNo is not None:
                    if programme.seasonNo is None:
                        (show_title_sans_season, programme.seasonNo) = parse_season_number(programme.title)
                        programme_dict['title'] = [(show_title_sans_season, u'')]
                    programme_dict['episode-num'] = [
                        (f'{(programme.seasonNo or 1) - 1} . {(programme.episodeNo or 1) - 1} . 0', u'xmltv_ns')]

                writer.addProgramme(programme_dict)

        writer.write(guide_file, True)
    gzip_file("public/magioGuide.xmltv")


def refresh():
    channels = magio.channels()
    generate_m3u8(channels)
    generate_xmltv(channels)
    print("Refreshing finished!")
    global last_refresh
    last_refresh = datetime.datetime.now().strftime("%d/%m/%Y %H:%M:%S")


# Config
qualityString = os.environ.get('MAGIO_QUALITY', "HIGH")
qualityMapping = {"LOW": MagioQuality.low, "MEDIUM": MagioQuality.medium, "HIGH": MagioQuality.high, "EXTRA": MagioQuality.extra}
quality = qualityMapping[qualityString]
print(f"Stream quality: {qualityString} ({quality})")
device_type = os.environ.get('MAGIO_DEVICE_TYPE', "OTT_STB")
print(f"Device type: {device_type}")
device_name = os.environ.get('MAGIO_DEVICE_NAME', "Magio IPTV Server")
print(f"Device name: {device_name}")

magio_username = os.environ.get('MAGIO_USERNAME', '1eabfvjdpp')
magio_password = os.environ.get('MAGIO_PASSWORD', 'Miriamka510')

print("Logging in to Magio Go TV")
magio = MagioGo("./storage", magio_username, magio_password, quality, device_type, device_name)
refresh()

scheduler = BackgroundScheduler(timezone="UTC")
scheduler.add_job(refresh, 'interval', hours=int(os.environ.get('MAGIO_GUIDE_REFRESH_HOURS', 12)))
scheduler.start()
atexit.register(lambda: scheduler.shutdown())
