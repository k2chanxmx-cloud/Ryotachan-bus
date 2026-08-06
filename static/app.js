const cards = document.getElementById('cards');
const statusEl = document.getElementById('status');
const updatedEl = document.getElementById('updated');
const countdownEl = document.getElementById('countdown');
const reloadBtn = document.getElementById('reload');
const noteEl = document.getElementById('note');
let remaining = REFRESH_SECONDS;
let loading = false;

function escapeHtml(value) {
  return String(value ?? '').replace(/[&<>'"]/g, ch => ({'&':'&amp;','<':'&lt;','>':'&gt;',"'":'&#39;','"':'&quot;'}[ch]));
}

function cardHtml(item) {
  const stops = item.stopsAway === 0 ? 'まもなく到着' : `<strong>${item.stopsAway}</strong> 停前`;
  const delay = item.delayMinutes > 0 ? `<span class="delay">約${item.delayMinutes}分遅れ</span>` : '<span>リアルタイム</span>';
  return `<article class="card">
    <div class="topline"><span class="route">${escapeHtml(item.route)}</span><span class="destination">${escapeHtml(item.destination)}行</span></div>
    <div class="mainrow"><div class="minutes">${item.minutes}<small>分</small></div><div class="stops">${stops}</div></div>
    <div class="details"><span>${escapeHtml(item.location)}付近</span><span>${escapeHtml(item.arrivalTime)}ごろ・${delay}</span></div>
  </article>`;
}

async function loadArrivals() {
  if (loading) return;
  loading = true;
  reloadBtn.disabled = true;
  statusEl.textContent = '最新の車両位置を取得しています…';
  try {
    const response = await fetch('/api/arrivals', {cache:'no-store'});
    const data = await response.json();
    if (!response.ok || !data.ok) throw new Error(data.error || '取得に失敗しました');
    cards.innerHTML = data.arrivals.length ? data.arrivals.map(cardHtml).join('') : `<div class="empty">${escapeHtml(data.message)}</div>`;
    statusEl.textContent = data.arrivals.length ? `到着が早い順に ${data.arrivals.length}台表示` : '';
    updatedEl.textContent = `最終更新 ${data.updatedAt}`;
    noteEl.textContent = data.estimateNote || '';
    remaining = data.refreshSeconds || REFRESH_SECONDS;
  } catch (error) {
    cards.innerHTML = `<div class="error"><strong>バス情報を取得できませんでした</strong><br><small>${escapeHtml(error.message)}</small></div>`;
    statusEl.textContent = '時間をおいて再度更新してください。';
  } finally {
    loading = false;
    reloadBtn.disabled = false;
  }
}

reloadBtn.addEventListener('click', loadArrivals);
setInterval(() => {
  remaining -= 1;
  countdownEl.textContent = `${Math.max(remaining,0)}秒後に更新`;
  if (remaining <= 0) loadArrivals();
}, 1000);

if ('serviceWorker' in navigator) navigator.serviceWorker.register('/static/sw.js').catch(() => {});
loadArrivals();
