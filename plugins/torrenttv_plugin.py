# -*- coding: utf-8 -*-
'''
Torrent-tv.ru Playlist Downloader Plugin
http://ip:port/ttvplaylist
'''

__author__ = 'AndreyPavlenko, Dorik1972'

import traceback
import gevent
import logging, re
import time
import hashlib
import requests
try: from urlparse import parse_qs
except: from urllib.parse import parse_qs
from PluginInterface import AceProxyPlugin
from PlaylistGenerator import PlaylistGenerator
import config.torrenttv as config
import config.p2pproxy as p2pconfig
from torrenttv_api import TorrentTvApi

class Torrenttv(AceProxyPlugin):

    # ttvplaylist handler is obsolete
    handlers = ('torrenttv', 'ttvplaylist',)

    def __init__(self, AceConfig, AceStuff):
        self.logger = logging.getLogger('torrenttv_plugin')
        self.lock = gevent.lock.Semaphore()
        self.channels = self.playlist = self.playlisttime = self.etag = self.tvgid = None
        self.logomap = config.logomap
        self.epg_id = { k:'' for k in self.logomap }
        self.updatelogos = p2pconfig.email != 're.place@me' and p2pconfig.password != 'ReplaceMe'

        if config.updateevery: gevent.spawn(self.playlistTimedDownloader)

    def playlistTimedDownloader(self):
        while 1:
            with self.lock: self.downloadPlaylist()
            gevent.sleep(config.updateevery * 60)

    def downloadPlaylist(self):
        headers = {'User-Agent': 'Magic Browser'}
        try:
            origin = requests.get(config.url, headers=headers, proxies=config.proxies, timeout=30).text

            self.logger.info('TTV playlist %s downloaded' % config.url)
            self.playlisttime = int(time.time())
            self.playlist = PlaylistGenerator(m3uchanneltemplate=config.m3uchanneltemplate)
            self.channels = {}
            m = hashlib.md5()
            if self.updatelogos:
                try:
                    translations_list = TorrentTvApi(p2pconfig.email, p2pconfig.password).translations('all')
                    for channel in translations_list:
                        name = channel.getAttribute('name')
                        if channel.getAttribute('epg_id') not in ('0', '', ' '):
                           self.epg_id[name] = 'ttv%s' % channel.getAttribute('id')
                        if not name in self.logomap:
                           self.logomap[name] = config.logobase + channel.getAttribute('logo')

                    self.logger.debug("Logos updated")
                    self.updatelogos = False
                except: self.updatelogos = False # p2pproxy plugin seems not configured

            self.logger.debug('Generating requested m3u playlist')
            pattern = re.compile(r',(?P<name>.+) \((?P<group>.+)\)[\r\n]+(?P<url>[^\r\n]+)?')

            for match in pattern.finditer(origin, re.MULTILINE):
                itemdict = match.groupdict()
                name = itemdict.get('name')

                itemdict['logo'] = self.logomap.get(name, 'http://static.acestream.net/sites/acestream/img/ACE-logo.png')
                itemdict['tvgid'] = self.epg_id.get(name, '')

                url = itemdict['url']
                if url.startswith(('acestream://', 'infohash://')) \
                      or (url.startswith(('http://','https://')) and url.endswith(('.acelive','.acestream','.acemedia'))):
                    self.channels[name] = url
                    itemdict['url'] = requests.compat.quote(name.encode('utf-8'),'') + '.ts'

                self.playlist.addItem(itemdict)
                m.update(name.encode('utf-8'))

            self.etag = '"' + m.hexdigest() + '"'

        except requests.exceptions.ConnectionError: self.logger.error("Can't download TTV playlist!"); return False
        except: self.logger.error(traceback.format_exc()); return False

        return True

    def handle(self, connection, headers_only=False):
        play = False
        with self.lock:
            # 30 minutes cache
            if not self.playlist or (int(time.time()) - self.playlisttime > 30 * 60):
                self.updatelogos = p2pconfig.email != 're.place@me' and p2pconfig.password != 'ReplaceMe'
                if not self.downloadPlaylist(): connection.dieWithError(); return

            url = requests.compat.urlparse(connection.path)
            path = url.path[0:-1] if url.path.endswith('/') else url.path
            params = parse_qs(connection.query)

            if path.startswith('/torrenttv/channel/'):
                if not path.endswith('.ts'):
                    connection.dieWithError(404, 'Invalid path: %s' % requests.compat.unquote(path), logging.ERROR)
                    return
                try: name = requests.compat.unquote(path.rsplit('/', 1)[1][:-3]).decode('utf-8')
                except AttributeError: name = requests.compat.unquote(path.rsplit('/', 1)[1][:-3])
                url = self.channels.get(name, None)
                if url is None:
                    connection.dieWithError(404, 'Unknown channel: ' + name, logging.ERROR); return
                elif url.startswith('acestream://'):
                    connection.path = '/content_id/%s/stream.ts' % url.split('/')[2]
                elif url.startswith('infohash://'):
                    connection.path = '/infohash/%s/stream.ts' % url.split('/')[2]
                elif url.startswith(('http://', 'https://')) and url.endswith(('.acelive', '.acestream', '.acemedia')):
                    connection.path = '/url/%s/stream.ts' % requests.compat.quote(url,'')
                connection.splittedpath = connection.path.split('/')
                connection.reqtype = connection.splittedpath[1].lower()
                play = True
            elif self.etag == connection.headers.get('If-None-Match'):
                self.logger.debug('ETag matches - returning 304')
                connection.send_response(304)
                connection.send_header('Connection', 'close')
                connection.end_headers()
                return
            else:
                self.logger.debug('Exporting m3u playlist')
                hostport = connection.headers['Host']
                path = '' if len(self.channels) == 0 else '/torrenttv/channel'
                add_ts = True if path.endswith('/ts') else False
                exported = self.playlist.exportm3u(hostport=hostport, path=path, add_ts=add_ts, header=config.m3uheadertemplate, fmt=params.get('fmt', [''])[0]).encode('utf-8')

                connection.send_response(200)
                connection.send_header('Content-Type', 'application/x-mpegurl')
                connection.send_header('Access-Control-Allow-Origin', '*')
                connection.send_header('ETag', self.etag)
                connection.send_header('Content-Length', str(len(exported)))
                connection.send_header('Connection', 'close')
                connection.end_headers()

        if play: connection.handleRequest(headers_only, name, config.logomap.get(name), fmt=params.get('fmt', [''])[0])
        elif not headers_only: connection.wfile.write(exported)
