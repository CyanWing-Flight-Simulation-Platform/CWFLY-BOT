/**
 * FSD 在线状态观察器（HTTP 轮询版）
 *
 * 取代原先「机器人自己用 FSD9 协议连 FSD」的做法：
 *   - 后端负责与 FSD 交互（/api/online/snapshot 读 FlightController/FlightPilot）
 *   - 机器人只轮询后端接口，本地做快照 diff，向 index.js 抛同样的事件
 *
 * 事件（与原 FSD9Client 保持一致）：
 *   connect / ready
 *   controller:add(ctrl) / controller:remove(ctrl) / pilot:add(p) / pilot:remove(p)
 *   controller:update({ controller, changes: { frequency: {from,to} } })
 *
 * 注意：FSD 的 ATIS 只存内存，不经协议连接取不到，因此 atis 字段恒为空字符串。
 */
import { EventEmitter } from 'node:events'

const API_BASE = (process.env.CWFLY_API_URL || 'http://127.0.0.1:3001/api').replace(/\/+$/, '')
const API_KEY = (process.env.CWFLY_API_KEY || '').trim()
const POLL_MS = Math.max(3000, Number(process.env.FSD_POLL_MS || 10000))

function isSystemCallsign(callsign) {
  const cs = (callsign || '').toUpperCase()
  // CW-* 为系统连接；TEST* 为联调测试用呼号，绝不允许出现在播报/列表里
  return cs.startsWith('CW-') || /^TEST/i.test(cs)
}

class FsdHttpObserver extends EventEmitter {
  constructor() {
    super()
    this._pilots = new Map()
    this._controllers = new Map()
    this._ready = false
    this._timer = null
    this._polling = false
    this._startedAt = 0
  }

  get isReady() {
    return this._ready
  }

  get pilotList() {
    return [...this._pilots.values()]
  }

  get controllerList() {
    return [...this._controllers.values()]
  }

  getPilot(callsign) {
    return this._pilots.get(String(callsign || '').toUpperCase()) || null
  }

  getController(callsign) {
    return this._controllers.get(String(callsign || '').toUpperCase()) || null
  }

  start() {
    if (this._timer) return
    this._startedAt = Date.now()
    this.poll().catch(() => {})
    this._timer = setInterval(() => {
      this.poll().catch(() => {})
    }, POLL_MS)
    if (typeof this._timer.unref === 'function') this._timer.unref()
  }

  stop() {
    if (this._timer) clearInterval(this._timer)
    this._timer = null
  }

  async poll() {
    if (this._polling) return
    this._polling = true
    try {
      const headers = { Accept: 'application/json', 'User-Agent': 'Mozilla/5.0 (compatible; CWFLY-Bot/1.0; +https://www.cwflysim.cn)' }
      if (API_KEY) headers['X-API-Key'] = API_KEY
      const res = await fetch(`${API_BASE}/online/snapshot`, {
        headers,
        signal: AbortSignal.timeout(8000),
      })
      if (!res.ok) throw new Error(`snapshot HTTP ${res.status}`)
      const body = await res.json()
      const data = body?.data || {}
      this._apply(data)
      if (!this._ready) {
        this._ready = true
        this.emit('connect')
        this.emit('ready')
        console.log(
          `[Observer] 后端快照就绪：机组 ${this._pilots.size} / 管制 ${this._controllers.size}`
        )
      }
    } finally {
      this._polling = false
    }
  }

  _apply(data) {
    const now = Date.now()
    const nextControllers = new Map()
    const nextPilots = new Map()

    for (const raw of data.controllers || []) {
      const key = String(raw.callsign || '').toUpperCase()
      if (!key || isSystemCallsign(key)) continue
      const prev = this._controllers.get(key)
      const ctrl = {
        callsign: raw.callsign,
        cid: String(raw.cid ?? ''),
        realname: raw.realname || '',
        rating: Number(raw.rating || 0),
        frequency: Number(raw.frequency || 0),
        facilitytype: Number(raw.facilitytype || 0),
        visualrange: Number(raw.visualrange || 40),
        lat: Number(raw.lat || 0),
        lon: Number(raw.lon || 0),
        atis: '',
        starttime: Number(raw.starttime || 0),
        lastSeen: now,
      }
      nextControllers.set(key, ctrl)
      if (!prev) {
        this.emit('controller:add', ctrl)
      } else if (Number(prev.frequency) !== ctrl.frequency) {
        this.emit('controller:update', {
          controller: ctrl,
          changes: { frequency: { from: prev.frequency, to: ctrl.frequency } },
        })
      }
    }

    for (const raw of data.pilots || []) {
      const key = String(raw.callsign || '').toUpperCase()
      if (!key || isSystemCallsign(key)) continue
      const pilot = {
        callsign: raw.callsign,
        cid: String(raw.cid ?? ''),
        realname: raw.realname || '',
        rating: Number(raw.rating || 0),
        aircraft: raw.aircraft || '',
        lat: Number(raw.lat || 0),
        lon: Number(raw.lon || 0),
        altitude: Number(raw.altitude || 0),
        groundspeed: Number(raw.groundspeed || 0),
        transponder: Number(raw.transponder || 0),
        plan: raw.plan || null,
        starttime: Number(raw.starttime || 0),
        lastSeen: now,
      }
      nextPilots.set(key, pilot)
      if (!this._pilots.has(key)) this.emit('pilot:add', pilot)
    }

    for (const [key, prev] of this._controllers) {
      if (!nextControllers.has(key)) this.emit('controller:remove', prev)
    }
    for (const [key, prev] of this._pilots) {
      if (!nextPilots.has(key)) this.emit('pilot:remove', prev)
    }

    this._controllers = nextControllers
    this._pilots = nextPilots
  }
}

let singleton = null

export function getObserver() {
  if (!singleton) singleton = new FsdHttpObserver()
  return singleton
}

export function getObserverStatus() {
  const o = getObserver()
  if (!o.isReady) return null
  const pilots = o.pilotList.filter((p) => !isSystemCallsign(p.callsign))
  const controllers = o.controllerList.filter((c) => !isSystemCallsign(c.callsign))
  return { pilots, controllers, clients: [...pilots, ...controllers], source: 'http' }
}

export { FsdHttpObserver }
