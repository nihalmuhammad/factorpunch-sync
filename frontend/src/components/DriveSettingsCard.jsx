import { useCallback, useEffect, useRef, useState } from 'react'
import { api } from '../api'

const input = 'w-full border border-gray-300 rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500'

// Admin-only. Signs the app in to Google Drive with the device flow: Google
// shows a short code, the admin types it at google.com/device on any phone or
// PC, and the server keeps the long-lived sign-in. Nothing secret ever comes
// back to the browser.
export default function DriveSettingsCard({ onToast }) {
  const [cfg, setCfg] = useState(null)
  const [form, setForm] = useState(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const onToastRef = useRef(onToast)

  const load = useCallback(() => {
    api.drive.status().then(setCfg).catch(() => {})
  }, [])

  useEffect(() => { onToastRef.current = onToast })
  useEffect(() => { load() }, [load])

  // While a sign-in is pending, poll Google at the interval it asked for.
  // Each poll returns a fresh cfg, which re-arms this effect; a reload
  // mid sign-in picks it straight back up from cfg.pending.
  useEffect(() => {
    if (!cfg?.pending) return undefined
    const timer = setTimeout(async () => {
      try {
        const data = await api.drive.poll()
        setCfg(data)
        if (data.status === 'connected') onToastRef.current('Google Drive connected')
      } catch (err) {
        setError(err.message)
        load()
      }
    }, (cfg.pending.interval || 5) * 1000)
    return () => clearTimeout(timer)
  }, [cfg, load])

  function startEdit() {
    setError('')
    setForm({ client_id: cfg.client_id || '', client_secret: '', folder_name: cfg.folder_name || '' })
  }

  async function handleSave(e) {
    e.preventDefault()
    setBusy(true)
    setError('')
    try {
      const payload = { ...form }
      if (!payload.client_secret) delete payload.client_secret
      setCfg(await api.drive.update(payload))
      setForm(null)
      onToast('Google Drive settings saved')
    } catch (err) {
      setError(err.message)
    } finally {
      setBusy(false)
    }
  }

  async function handleConnect() {
    setBusy(true)
    setError('')
    try {
      setCfg(await api.drive.connect())
    } catch (err) {
      setError(err.message)
    } finally {
      setBusy(false)
    }
  }

  async function handleDisconnect() {
    setBusy(true)
    setError('')
    try {
      setCfg(await api.drive.disconnect())
      onToast('Google Drive disconnected')
    } catch (err) {
      setError(err.message)
    } finally {
      setBusy(false)
    }
  }

  if (!cfg) return null
  const hasClient = cfg.client_id && cfg.client_secret_set

  return (
    <div className="max-w-xl bg-white rounded-xl border border-gray-200 overflow-hidden mt-6" data-testid="drive-settings">
      <div className="flex items-center justify-between px-5 py-4 border-b border-gray-200">
        <div>
          <p className="font-medium text-gray-900">Google Drive</p>
          <p className="text-xs text-gray-400 mt-0.5">
            Where the daily "Send attendance" button on the Attendance page saves its CSV.
          </p>
        </div>
        <span className={`text-xs font-medium px-2.5 py-1 rounded-full ${
          cfg.connected ? 'bg-green-100 text-green-700' : 'bg-gray-100 text-gray-500'
        }`}>
          {cfg.connected ? 'Connected' : 'Not connected'}
        </span>
      </div>

      <div className="px-5 py-4 space-y-3 text-sm">
        {form ? (
          <form onSubmit={handleSave} className="space-y-3">
            <div>
              <label className="block text-sm font-medium text-gray-700 mb-1">OAuth client ID</label>
              <input className={input} value={form.client_id}
                onChange={(e) => setForm({ ...form, client_id: e.target.value })}
                placeholder="1234-abc.apps.googleusercontent.com" />
            </div>
            <div>
              <label className="block text-sm font-medium text-gray-700 mb-1">
                OAuth client secret
                <span className="ml-1.5 text-xs font-normal text-gray-400">
                  {cfg.client_secret_set ? 'saved; leave blank to keep it' : ''}
                </span>
              </label>
              <input type="password" autoComplete="off" className={input} value={form.client_secret}
                onChange={(e) => setForm({ ...form, client_secret: e.target.value })} />
            </div>
            <div>
              <label className="block text-sm font-medium text-gray-700 mb-1">Drive folder name</label>
              <input className={input} value={form.folder_name} required
                onChange={(e) => setForm({ ...form, folder_name: e.target.value })} />
              <p className="text-xs text-gray-400 mt-1">The app creates this folder in the signed-in account's Drive.</p>
            </div>
            <p className="text-xs text-amber-700">Changing the client ID or secret signs Google Drive out.</p>
            <div className="flex gap-2">
              <button type="submit" disabled={busy}
                className="bg-blue-600 hover:bg-blue-700 disabled:opacity-50 text-white text-sm font-medium px-4 py-2 rounded-lg">
                {busy ? 'Saving…' : 'Save'}
              </button>
              <button type="button" onClick={() => setForm(null)}
                className="border border-gray-300 text-gray-600 text-sm font-medium px-4 py-2 rounded-lg">
                Cancel
              </button>
            </div>
          </form>
        ) : (
          <>
            <div className="flex justify-between"><span className="text-gray-500">OAuth client</span>
              <span className="text-gray-900 font-mono text-xs truncate max-w-[60%]">{cfg.client_id || '—'}</span></div>
            <div className="flex justify-between"><span className="text-gray-500">Folder</span>
              <span className="text-gray-900">{cfg.folder_name}</span></div>
            {cfg.connected && (
              <div className="flex justify-between"><span className="text-gray-500">Connected by</span>
                <span className="text-gray-900">{cfg.connected_by} · {new Date(cfg.connected_at).toLocaleDateString()}</span></div>
            )}
            {cfg.last_send_at && (
              <div className="flex justify-between"><span className="text-gray-500">Last send</span>
                <span className={cfg.last_send_ok ? 'text-gray-900' : 'text-red-600'}>
                  {cfg.last_send_ok ? 'OK' : 'Failed'} · {new Date(cfg.last_send_at).toLocaleString()}
                </span></div>
            )}
            {!cfg.last_send_ok && cfg.last_error && (
              <p className="text-xs text-red-500">{cfg.last_error}</p>
            )}

            {cfg.pending && (
              <div className="rounded-lg bg-blue-50 border border-blue-200 p-3">
                <p className="text-sm text-blue-900">
                  On any phone or computer, open{' '}
                  <a href={cfg.pending.verification_url} target="_blank" rel="noreferrer"
                    className="underline font-medium">{cfg.pending.verification_url}</a>{' '}
                  and enter this code:
                </p>
                <p className="mt-2 text-2xl font-mono font-semibold tracking-widest text-blue-900">
                  {cfg.pending.user_code}
                </p>
                <p className="mt-1 text-xs text-blue-700">Waiting for you to approve on the Google page…</p>
              </div>
            )}

            {!hasClient && (
              <details className="text-xs text-gray-500">
                <summary className="cursor-pointer text-blue-600">How to get an OAuth client ID (one time)</summary>
                <ol className="list-decimal ml-5 mt-2 space-y-1">
                  <li>In Google Cloud Console, create a project and enable the Google Drive API.</li>
                  <li>Set up the OAuth consent screen (External) and publish it "In production", otherwise the sign-in stops working after 7 days.</li>
                  <li>Under Credentials, create an OAuth client ID of type "TVs and Limited Input devices".</li>
                  <li>Paste the client ID and secret here with Edit, then press Connect.</li>
                </ol>
              </details>
            )}

            <div className="flex flex-wrap gap-2 pt-1">
              <button onClick={startEdit}
                className="border border-gray-300 hover:bg-gray-50 text-gray-700 text-sm font-medium px-4 py-2 rounded-lg">
                Edit
              </button>
              {hasClient && !cfg.connected && !cfg.pending && (
                <button onClick={handleConnect} disabled={busy}
                  className="bg-blue-600 hover:bg-blue-700 disabled:opacity-50 text-white text-sm font-medium px-4 py-2 rounded-lg">
                  {busy ? 'Starting…' : 'Connect Google Drive'}
                </button>
              )}
              {(cfg.connected || cfg.pending) && (
                <button onClick={handleDisconnect} disabled={busy}
                  className="border border-red-200 hover:bg-red-50 text-red-600 text-sm font-medium px-4 py-2 rounded-lg">
                  {cfg.pending ? 'Cancel sign-in' : 'Disconnect'}
                </button>
              )}
            </div>
          </>
        )}
        {error && <p className="text-sm text-red-600">{error}</p>}
      </div>
    </div>
  )
}
