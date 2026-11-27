import { useCallback, useEffect, useState } from 'react'
import { api } from '../api'

// "2026-11-27" -> "27 Nov 2026" without Date(), so the browser's timezone
// can never shift the day.
const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec']
function formatDay(iso) {
  if (!iso) return ''
  const [y, m, d] = iso.split('-')
  return `${Number(d)} ${MONTHS[Number(m) - 1]} ${y}`
}

function formatWhen(value) {
  if (!value) return ''
  return new Date(value).toLocaleString(undefined, {
    day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit',
  })
}

// The button staff press once a day. Sends the shift-aware CSV for one
// operational day to the Google Drive folder an admin connected in Settings.
export default function SendToDriveCard() {
  const [status, setStatus] = useState(null)
  const [day, setDay] = useState('')
  const [sending, setSending] = useState(false)
  const [message, setMessage] = useState(null)

  const load = useCallback(() => {
    api.drive.status().then((data) => {
      setStatus(data)
      setDay((current) => current || data.today)
    }).catch(() => {})
  }, [])

  useEffect(() => { load() }, [load])

  async function handleSend() {
    setSending(true)
    setMessage(null)
    try {
      const data = await api.drive.send(day)
      setStatus(data)
      setMessage({
        type: 'success',
        text: data.replaced
          ? `Sent. ${data.last_send_file} was updated in Google Drive.`
          : `Sent. ${data.last_send_file} was saved to Google Drive.`,
      })
    } catch (err) {
      setMessage({ type: 'error', text: err.message })
      load()
    } finally {
      setSending(false)
    }
  }

  if (!status) return null

  return (
    <div className="bg-white rounded-xl border border-gray-200 px-5 py-4 mb-6" data-testid="send-to-drive">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <p className="font-medium text-gray-900">Send attendance to Google Drive</p>
          <p className="text-xs text-gray-400 mt-0.5">
            {status.connected
              ? `Saves attendance-${day || status.today}.csv to the "${status.folder_name}" folder in ${status.allowed_account}'s Google Drive. Sending again the same day replaces the file.`
              : "Google Drive isn't set up yet. Ask an admin to connect it in Settings."}
          </p>
        </div>
        <div className="flex items-center gap-2">
          <input
            type="date"
            value={day}
            max={status.today}
            onChange={(e) => setDay(e.target.value)}
            className="border border-gray-300 rounded-lg px-2 py-1.5 text-sm"
            aria-label="Day to send"
          />
          <button
            onClick={handleSend}
            disabled={sending || !status.connected || !day}
            className="bg-blue-600 hover:bg-blue-700 disabled:opacity-50 text-white text-sm font-medium px-4 py-2 rounded-lg transition-colors"
          >
            {sending ? 'Sending…' : `Send ${day === status.today ? "today's" : formatDay(day)} attendance`}
          </button>
        </div>
      </div>

      {message && (
        <p className={`mt-3 text-sm ${message.type === 'error' ? 'text-red-600' : 'text-green-700'}`}>
          {message.text}
        </p>
      )}

      {!message && status.last_send_at && (
        <p className={`mt-3 text-xs ${status.last_send_ok ? 'text-gray-500' : 'text-red-600'}`}>
          {status.last_send_ok
            ? `Last sent ${formatWhen(status.last_send_at)} by ${status.last_send_by}: ${status.last_send_file}`
            : `Last send failed ${formatWhen(status.last_send_at)}: ${status.last_error}`}
        </p>
      )}
    </div>
  )
}
