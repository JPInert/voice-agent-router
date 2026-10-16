"""Kodi (the TV box): what is playing. Read only.

Playback commands ("play X", pause, next episode) are tier-0 reflexes, not model tools: they sit in
tricks.yaml as `kodi:` actions, where a phrase either matches or does nothing. The model only reads.
"""
import base64
import json
import urllib.request


class HttpKodi:
    """Kodi's JSON-RPC over HTTP. url like http://kodi.local:8080/jsonrpc; user/password optional."""

    def __init__(self, url, user='', password='', timeout=8):
        self.url, self.timeout, self.hdrs = url, timeout, {'Content-Type': 'application/json'}
        if user:
            self.hdrs['Authorization'] = 'Basic ' + base64.b64encode(f'{user}:{password}'.encode()).decode()

    def rpc(self, method, params=None):
        body = {'jsonrpc': '2.0', 'id': 1, 'method': method, 'params': params or {}}
        req = urllib.request.Request(self.url, data=json.dumps(body).encode(), headers=self.hdrs, method='POST')
        with urllib.request.urlopen(req, timeout=self.timeout) as r:
            return json.loads(r.read()).get('result')


def now_playing(kodi):
    players = kodi.rpc('Player.GetActivePlayers') or []
    if not players:
        return 'The TV (Kodi) is not playing anything.'
    pid = players[0]['playerid']
    item = (kodi.rpc('Player.GetItem', {'playerid': pid, 'properties': ['title', 'showtitle', 'season', 'episode']})
            or {}).get('item', {})
    prog = kodi.rpc('Player.GetProperties', {'playerid': pid, 'properties': ['percentage', 'speed']}) or {}
    what = (f"{item.get('showtitle')} season {item.get('season')} episode {item.get('episode')}, {item.get('title')}"
            if item.get('showtitle') else item.get('title') or item.get('label') or 'something')
    return f"Kodi: {what}, {round(prog.get('percentage') or 0)}% in, {'paused' if prog.get('speed') == 0 else 'playing'}."


def register(reg, kodi):
    reg.add('now_playing', 'What the TV (Kodi) is playing right now.', {}, lambda: now_playing(kodi), source='kodi')
