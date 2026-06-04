# Minimal drop-in xmltv.Writer implementation
# Replaces the unmaintained 'xmltv' PyPI package.
# Supports: Writer, addChannel, addProgramme, write

import xml.etree.ElementTree as ET
from xml.dom import minidom


def _text(parent, tag, text, attrib=None):
    el = ET.SubElement(parent, tag, attrib or {})
    el.text = str(text) if text is not None else ''
    return el


class Writer:
    def __init__(self, date='', generator_info_name='', generator_info_url='',
                 source_info_name='', source_info_url=''):
        self._root = ET.Element('tv', {
            'date': date,
            'generator-info-name': generator_info_name,
            'generator-info-url': generator_info_url,
            'source-info-name': source_info_name,
            'source-info-url': source_info_url,
        })

    def addChannel(self, channel):
        """
        channel dict keys used:
          id, display-name [(text, lang), ...], icon [{'src': url}, ...]
        """
        el = ET.SubElement(self._root, 'channel', {'id': str(channel.get('id', ''))})
        for (name, lang) in channel.get('display-name', []):
            _text(el, 'display-name', name, {'lang': lang} if lang else {})
        for icon in channel.get('icon', []):
            ET.SubElement(el, 'icon', {'src': icon.get('src', '')})

    def addProgramme(self, programme):
        """
        programme dict keys used:
          channel, start, stop, title, desc, date, category, icon,
          length, episode-num, credits
        """
        attrib = {
            'start': programme.get('start', ''),
            'stop': programme.get('stop', ''),
            'channel': str(programme.get('channel', '')),
        }
        el = ET.SubElement(self._root, 'programme', attrib)

        for (title, lang) in programme.get('title', []):
            _text(el, 'title', title, {'lang': lang} if lang else {})

        for (desc, lang) in programme.get('desc', []):
            _text(el, 'desc', desc, {'lang': lang} if lang else {})

        date = programme.get('date')
        if date and str(date) not in ('None', ''):
            _text(el, 'date', date)

        for (cat, lang) in programme.get('category', []):
            _text(el, 'category', cat, {'lang': lang} if lang else {})

        for icon in programme.get('icon', []):
            src = icon.get('src', '') if isinstance(icon, dict) else str(icon)
            if src:
                ET.SubElement(el, 'icon', {'src': src})

        length = programme.get('length')
        if length:
            _text(el, 'length', length.get('length', ''), {'units': length.get('units', 'seconds')})

        for (epnum, system) in programme.get('episode-num', []):
            _text(el, 'episode-num', epnum, {'system': system} if system else {})

        credits = programme.get('credits', {})
        if any(credits.values()):
            cred_el = ET.SubElement(el, 'credits')
            for director in credits.get('director', []):
                _text(cred_el, 'director', director)
            for actor in credits.get('actor', []):
                _text(cred_el, 'actor', actor)
            for writer in credits.get('writer', []):
                _text(cred_el, 'writer', writer)
            for producer in credits.get('producer', []):
                _text(cred_el, 'producer', producer)

    def write(self, file_obj, pretty_print=False):
        tree = ET.ElementTree(self._root)
        if pretty_print:
            raw = ET.tostring(self._root, encoding='unicode', xml_declaration=False)
            reparsed = minidom.parseString(f'<?xml version="1.0" encoding="UTF-8"?>{raw}')
            output = reparsed.toprettyxml(indent='  ', encoding='utf-8')
            file_obj.write(output)
        else:
            tree.write(file_obj, encoding='utf-8', xml_declaration=True)
