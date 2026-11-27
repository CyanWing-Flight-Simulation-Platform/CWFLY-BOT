"""FSD 在线状态观察器（Python 版，HTTP 轮询）

与 Node 版 `src/services/fsdHttp.js` 行为一致：轮询后端 `/api/online/snapshot`，
本地 diff 后抛 controller:add/remove/update、pilot:add/remove 事件。

原 `py/fsd9.py`（协议直连）已停用；机器人不再与 FSD 建立连接。
"""
import asyncio
import time

from .. import config
from . import http

API_BASE = (getattr(config, 'CWFLY_API_URL', '') or 'http://127.0.0.1:3001/api').rstrip('/')
API_KEY = (getattr(config, 'CWFLY_API_KEY', '') or '').strip()
POLL_SECONDS = max(3, int(getattr(config, 'FSD_POLL_SECONDS', 10)))


def is_system_callsign(callsign):
    cs = (callsign or '').upper()
    # CW-* 为系统连接；TEST* 为联调测试用呼号，绝不允许出现在播报/列表里
    return cs.startswith('CW-') or cs.startswith('TEST')


class FsdHttpObserver:
    def __init__(self):
        self._pilots = {}
        self._controllers = {}
        self._listeners = {}
        self._ready = False
        self._task = None

    # ---- 事件 ----
    def on(self, event, handler):
        self._listeners.setdefault(event, []).append(handler)

    def _emit(self, event, payload=None):
        for handler in self._listeners.get(event, []):
            try:
                # 无载荷事件（connect/ready）按零参数调用，兼容无参处理器
                handler() if payload is None else handler(payload)
            except Exception as exc:  # noqa: BLE001
                print(f'[Observer] listener error ({event}): {exc}')

    # ---- 状态 ----
    @property
    def is_ready(self):
        return self._ready

    @property
    def pilot_list(self):
        return list(self._pilots.values())

    @property
    def controller_list(self):
        return list(self._controllers.values())

    def get_pilot(self, callsign):
        return self._pilots.get(str(callsign or '').upper())

    def get_controller(self, callsign):
        return self._controllers.get(str(callsign or '').upper())

    # ---- 生命周期 ----
    async def start(self):
        if self._task is not None:
            return
        self._task = asyncio.create_task(self._loop())

    async def _loop(self):
        while True:
            try:
                await self.poll()
            except Exception as exc:  # noqa: BLE001
                print(f'[Observer] 轮询失败: {exc}')
            await asyncio.sleep(POLL_SECONDS)

    async def poll(self):
        headers = {'Accept': 'application/json', 'User-Agent': 'Mozilla/5.0 (compatible; CWFLY-Bot/1.0; +https://www.cwflysim.cn)'}
        if API_KEY:
            headers['X-API-Key'] = API_KEY
        resp = await http.http_get(f'{API_BASE}/online/snapshot', timeout=8, headers=headers)
        if resp.status_code != 200:
            raise RuntimeError(f'snapshot HTTP {resp.status_code}')
        payload = resp.json() or {}
        self._apply(payload.get('data') or {})
        if not self._ready:
            self._ready = True
            self._emit('connect')
            self._emit('ready')
            print(f'[Observer] 快照就绪：机组 {len(self._pilots)} / 管制 {len(self._controllers)}')

    def _apply(self, data):
        now = time.time()
        next_controllers = {}
        next_pilots = {}

        for raw in data.get('controllers') or []:
            key = str(raw.get('callsign') or '').upper()
            if not key or is_system_callsign(key):
                continue
            prev = self._controllers.get(key)
            ctrl = {
                'callsign': raw.get('callsign'),
                'cid': str(raw.get('cid') or ''),
                'realname': raw.get('realname') or '',
                'rating': int(raw.get('rating') or 0),
                'frequency': int(raw.get('frequency') or 0),
                'facilitytype': int(raw.get('facilitytype') or 0),
                'visualrange': int(raw.get('visualrange') or 40),
                'lat': float(raw.get('lat') or 0),
                'lon': float(raw.get('lon') or 0),
                'atis': '',
                'starttime': int(raw.get('starttime') or 0),
                'lastSeen': now,
            }
            next_controllers[key] = ctrl
            if prev is None:
                self._emit('controller:add', ctrl)
            elif int(prev.get('frequency') or 0) != ctrl['frequency']:
                self._emit('controller:update', {
                    'controller': ctrl,
                    'changes': {'frequency': {'from': prev.get('frequency'), 'to': ctrl['frequency']}},
                })

        for raw in data.get('pilots') or []:
            key = str(raw.get('callsign') or '').upper()
            if not key or is_system_callsign(key):
                continue
            pilot = {
                'callsign': raw.get('callsign'),
                'cid': str(raw.get('cid') or ''),
                'realname': raw.get('realname') or '',
                'rating': int(raw.get('rating') or 0),
                'aircraft': raw.get('aircraft') or '',
                'lat': float(raw.get('lat') or 0),
                'lon': float(raw.get('lon') or 0),
                'altitude': int(raw.get('altitude') or 0),
                'groundspeed': int(raw.get('groundspeed') or 0),
                'transponder': int(raw.get('transponder') or 0),
                'plan': raw.get('plan') or None,
                'starttime': int(raw.get('starttime') or 0),
                'lastSeen': now,
            }
            next_pilots[key] = pilot
            if key not in self._pilots:
                self._emit('pilot:add', pilot)

        for key, prev in self._controllers.items():
            if key not in next_controllers:
                self._emit('controller:remove', prev)
        for key, prev in self._pilots.items():
            if key not in next_pilots:
                self._emit('pilot:remove', prev)

        self._controllers = next_controllers
        self._pilots = next_pilots


_instance = None


def get_observer():
    global _instance
    if _instance is None:
        _instance = FsdHttpObserver()
    return _instance


async def start_observer():
    observer = get_observer()
    await observer.start()
    return observer


def get_observer_status():
    observer = get_observer()
    if not observer.is_ready:
        return None
    pilots = [p for p in observer.pilot_list if not is_system_callsign(p.get('callsign'))]
    controllers = [c for c in observer.controller_list if not is_system_callsign(c.get('callsign'))]
    return {'pilots': pilots, 'controllers': controllers,
            'clients': pilots + controllers, 'source': 'snapshot'}
