import { useCallback, useEffect, useRef, useState } from 'react'
import { api } from '../api'

const input = 'w-full border border-gray-300 rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500'
const btn = 'text-sm font-medium px-4 py-2 rounded-lg disabled:opacity-50'

function Row({ label, children }) {
  return (
    <div className="flex justify-between gap-4">
      <span className="text-gray-500 shrink-0">{label}</span>
      <span className="text-gray-900 text-right break-all">{children}</span>
    </div>
  )
}

// Admin-only. The app writes to the restaurant's Google shared drive as a
// service account: upload its key file once, add its email to the shared
// drive as Content manager, paste the shared drive's link. The private key
// never comes back to the browser; only the service account's email does.
export default function DriveSettingsCard({ onToast }) {
  const [cfg, setCfg] = useState(null)
  const [folder, setFolder] = useState('')
  const [busy, setBusy] = useState('')
  const [error, setError] = useState('')
  const fileInput = useRef(null)

  const load = useCallback(() => {
    api.drive.status().then(setCfg).catch(() => {})
  }, [])

  useEffect(() => { load() }, [load])

  async function run(name, action, success) {
    setBusy(name)
    setError('')
    try {
      const data = await action()
      setCfg(data)
      if (success) onToast(typeof success === 'function' ? success(data) : success)
      return true
    } catch (err) {
      setError(err.message)
      return false
    } finally {
      setBusy('')
    }
  }

  async function handleKeyFile(e) {
    const file = e.target.files?.[0]
    e.target.value = ''
    if (!file) return
    const text = await file.text()
    run('key', () => api.drive.uploadKey(text), 'Service-account key saved')
  }

  function handleFolder(e) {
    e.preventDefault()
    run('folder', () => api.drive.setFolder(folder), 'Shared drive folder saved')
      .then((ok) => { if (ok) setFolder('') })
  }

  if (!cfg) return null

  return (
    <div className="max-w-xl bg-white rounded-xl border border-gray-200 overflow-hidden mt-6" data-testid="drive-settings">
      <div className="flex items-center justify-between px-5 py-4 border-b border-gray-200">
        <div>
          <p className="font-medium text-gray-900">Google shared drive</p>
          <p className="text-xs text-gray-400 mt-0.5">
            Where the daily "Send attendance" button on the Attendance page saves its CSV.
          </p>
        </div>
        <span className={`text-xs font-medium px-2.5 py-1 rounded-full ${
          cfg.ready ? 'bg-green-100 text-green-700' : 'bg-gray-100 text-gray-500'
        }`}>
          {cfg.ready ? 'Ready' : 'Not set up'}
        </span>
      </div>

      <div className="px-5 py-4 space-y-4 text-sm">
        {/* Step 1: key */}
        <div className="space-y-2">
          <p className="font-medium text-gray-700">1. Service-account key</p>
          {cfg.key_set ? (
            <>
              <Row label="Service account">{cfg.sa_email}</Row>
              <Row label="Uploaded">
                {cfg.key_uploaded_by} · {new Date(cfg.key_uploaded_at).toLocaleDateString()}
              </Row>
              <p className="text-xs text-gray-500">
                In Google Drive, open the shared drive, choose Manage members, and add
                <span className="font-mono"> {cfg.sa_email} </span>as <strong>Content manager</strong>.
              </p>
            </>
          ) : (
            <p className="text-xs text-gray-500">
              Upload the JSON key file you downloaded for the service account in Google Cloud.
            </p>
          )}
          <div className="flex gap-2">
            <input ref={fileInput} type="file" accept=".json,application/json" className="hidden"
              onChange={handleKeyFile} data-testid="drive-key-file" />
            <button onClick={() => fileInput.current?.click()} disabled={!!busy}
              className={`${btn} border border-gray-300 hover:bg-gray-50 text-gray-700`}>
              {busy === 'key' ? 'Uploading…' : cfg.key_set ? 'Replace key' : 'Upload key file'}
            </button>
            {cfg.key_set && (
              <button onClick={() => run('remove', api.drive.removeKey, 'Key removed')} disabled={!!busy}
                className={`${btn} border border-red-200 hover:bg-red-50 text-red-600`}>
                Remove key
              </button>
            )}
          </div>
        </div>

        {/* Step 2: folder */}
        <div className="space-y-2 pt-3 border-t border-gray-100">
          <p className="font-medium text-gray-700">2. Shared drive</p>
          {cfg.folder_id && (
            <Row label="Folder">{cfg.folder_title || cfg.folder_id}</Row>
          )}
          <form onSubmit={handleFolder} className="flex gap-2">
            <input className={input} value={folder} onChange={(e) => setFolder(e.target.value)}
              placeholder="Paste the shared drive's link from the browser" />
            <button type="submit" disabled={!!busy || !folder.trim()}
              className={`${btn} bg-blue-600 hover:bg-blue-700 text-white shrink-0`}>
              {busy === 'folder' ? 'Saving…' : 'Save'}
            </button>
          </form>
        </div>

        {/* Step 3: test */}
        <div className="space-y-2 pt-3 border-t border-gray-100">
          <p className="font-medium text-gray-700">3. Check it works</p>
          <button onClick={() => run('test', api.drive.test, (d) => `Connected to "${d.folder_title}"`)}
            disabled={!!busy || !cfg.ready}
            className={`${btn} border border-gray-300 hover:bg-gray-50 text-gray-700`}>
            {busy === 'test' ? 'Checking…' : 'Test connection'}
          </button>
          {cfg.last_send_at && (
            <Row label="Last send">
              <span className={cfg.last_send_ok ? '' : 'text-red-600'}>
                {cfg.last_send_ok ? 'OK' : 'Failed'} · {new Date(cfg.last_send_at).toLocaleString()}
              </span>
            </Row>
          )}
          {!cfg.last_send_ok && cfg.last_error && (
            <p className="text-xs text-red-500">{cfg.last_error}</p>
          )}
        </div>

        {error && <p className="text-sm text-red-600">{error}</p>}
      </div>
    </div>
  )
}
