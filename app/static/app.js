import {OPENING_REFRESH_MS, OPENING_TIMEOUT_MS, openingPhase, openingUpdateMessage} from './opening_freshness.mjs';
import {budapestDay} from './calendar.mjs';
import {visibleRecommendationReasons} from './recommendations.mjs';
import {anchorCommercialClock, commercialState, commercialMessage} from './provider_freshness.mjs';

const $ = (selector, scope = document) => scope.querySelector(selector);
const $$ = (selector, scope = document) => [...scope.querySelectorAll(selector)];
const esc = (value) => String(value ?? '').replace(/[&<>"']/g, char => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[char]));
const state = {when:'today', category:'', config:null, planId:null, plan:null, pendingActivity:null, items:[], visible:12};
let listQuery = null;
let listSnapshot = null;
let listCopy = null;
let detailActivity = null;
let detailController;
let detailVersion = 0;
let calendarRequest = null;
let planDayEdited = false;
let planReadVersion = 0;
let planMutationPending = false;
let planRefreshError = '';
let startupReady = false;
let startRequest = null;
const artIds = {food:'coffee', massage:'wellness', events:'culture', nightlife:'culture'};
const labels = {reset:'A little reset', curious:'Something new', active:'Get moving', focus:'Focus time', social:'Better together', treat:'Me-time', quiet:'Quiet setting', study:'Study-friendly', indoor:'Indoors', outdoor:'Outdoors'};
let controller;
let toastTimer;

async function api(path, options = {}) {
  const startedAt = performance.now();
  let response;
  try {response = await fetch(path, {credentials:'same-origin', ...options, headers:{'Content-Type':'application/json', ...options.headers}});}
  catch (error) {
    if (error.name === 'AbortError' || error.name === 'TimeoutError') throw error;
    throw new Error('Could not connect. Check your connection and try again.');
  }
  let body;
  try {body = await response.json();}
  catch {
    const error = new Error('The server is temporarily unavailable. Please try again shortly.');
    error.status = response.status; throw error;
  }
  if (!response.ok) {
    const message = Array.isArray(body.detail) ? body.detail.map(item => item.msg).join('. ') : body.detail;
    const error = new Error(message || 'Something went wrong. Please try again.');
    error.status = response.status; throw error;
  }
  if (body.provider_freshness) anchorCommercialClock(body,startedAt);
  (body.items || []).forEach(a => anchorCommercialClock(a,startedAt));
  (body.candidates || []).forEach(c => anchorCommercialClock(c.activity,startedAt));
  return body;
}
async function openingApi(path, signal, timeoutMessage = 'Opening status update timed out. Please try again.') {
  const request = new AbortController();
  const cancel = () => request.abort(signal.reason);
  if (signal?.aborted) cancel();
  else signal?.addEventListener('abort', cancel, {once:true});
  const timeout = setTimeout(() => request.abort(new DOMException(timeoutMessage, 'TimeoutError')), OPENING_TIMEOUT_MS);
  try { return await api(path, {signal:request.signal, cache:'no-store'}); }
  finally {clearTimeout(timeout); signal?.removeEventListener('abort', cancel);}
}
async function currentDateApi(path, signal) {
  for (let attempt = 0; attempt < 2; attempt++) {
    const result = await openingApi(path, signal);
    const instant = result.as_of || result.opening?.as_of;
    if (!budapestDay(instant)) break;
    if (!isPreviousDay(instant)) return result;
  }
  throw new Error('The current Budapest date could not be checked. Please try again.');
}
const send = (path, body, method = 'POST') => api(path, {method, body:JSON.stringify(body)});
function notify(message) {
  const element = $('#toast'); element.textContent = message; element.hidden = false;
  clearTimeout(toastTimer); toastTimer = setTimeout(() => element.hidden = true, 4500);
}
function price(a) {
  if (['expired','unconfirmed'].includes(commercialState(a,'price'))) return 'Price needs rechecking';
  if (a.price_low == null) return a.published_price_label || 'Price to confirm';
  const format = number => new Intl.NumberFormat('en-GB').format(number);
  return `${format(a.price_low)}${a.price_high !== a.price_low ? `–${format(a.price_high)}` : ''} Ft`;
}
function renderCommercialViews() {
  const activities = new Map([...state.items, ...(state.plan?.candidates || []).map(c => c.activity)].map(a => [a.id,a]));
  $$('#activity-grid [data-activity-id], #plan-content [data-candidate-activity]').forEach(element => {
    const a = activities.get(element.dataset.activityId || element.dataset.candidateActivity);
    if (!a) return;
    const value = $('.price',element);
    if (value) {value.textContent = price(a); value.classList.toggle('unknown',a.price_low == null || commercialState(a,'price') !== 'current');}
    const student = $('[data-student-offer]',element);
    if (student) student.hidden = commercialState(a,'student_offer') !== 'current';
    const summary = $('.recommendation-summary',element);
    if (summary) {
      const reasons = visibleRecommendationReasons(a);
      summary.textContent = reasons[0];
      $('[data-recommendation-basis]',element).innerHTML = reasons.map(reason => `<li>${esc(reason)}</li>`).join('');
    }
  });
  if (!detailActivity || !$('#detail-dialog').open) return;
  $('.meta-price').textContent = price(detailActivity);
  for (const [field,selector,key] of [['price','.detail-price-note','price_note'],['student_offer','.detail-student-note','student_note']]) {
    const message = commercialMessage(detailActivity,field);
    const note = detailActivity[key] || '';
    $(selector).textContent = message && !note.startsWith(message) ? `${message} Previously published information: ${note}` : note;
  }
}
function visitTimeLabel(a) {
  if (a.duration_kind === 'flexible') return 'Choose your visit length';
  if (a.duration_kind === 'fixed') return `${a.duration} min session`;
  return 'Duration to confirm';
}
function visitTimeDetails(a) {
  let html = `<p><strong>${esc(visitTimeLabel(a))}</strong></p><p>${esc(a.duration_note)}</p>`;
  if (a.timing_source_url && a.timing_checked_on) html += `<p class="timing-source">${external(a.timing_source_url,'Activity format source','source-link')} · checked ${esc(a.timing_checked_on)}.</p>`;
  if (a.visit_estimate_minutes) {
    const note = a.visit_estimate_note || `Suggested visit: around ${a.visit_estimate_minutes} minutes; this is not a fixed session or minimum stay.`;
    html += `<p><strong>Suggested visit:</strong> ${esc(note)}</p>`;
  }
  if (a.visit_limit_minutes) {
    html += `<p><strong>Ticket time limit:</strong> ${esc(a.visit_limit_minutes)} minutes.</p><p>${esc(a.visit_limit_note)}</p>`;
  }
  return html;
}
function budapestTime(value) {
  const date = new Date(value);
  if (!Number.isFinite(date.getTime())) return 'unavailable';
  return new Intl.DateTimeFormat('en-GB',{timeZone:'Europe/Budapest',weekday:'short',hour:'2-digit',minute:'2-digit'}).format(date);
}
function openingBadges(a) {
  const o = a.opening;
  if (!o) return '';
  if (openingPhase(a) !== 'fresh') return `<span class="tag opening-unknown">Current status unavailable</span>${o.appointment_required ? '<span class="tag">Appointment required</span>' : ''}`;
  return `<span class="tag opening-${esc(o.state)}">${esc(o.label)}${o.state === 'open' ? ' · published hours' : ''}</span>${o.appointment_required ? '<span class="tag">Appointment required</span>' : ''}`;
}
function currentVisitBadge(a) {
  const v = a.visit_now;
  if (!v || a.opening?.state !== 'open' || openingPhase(a) !== 'fresh') return '';
  const colour = v.state === 'fits' ? 'open' : v.state === 'does_not_fit' ? 'closed' : 'unknown';
  return `<span class="tag opening-${colour}">${esc(v.label)}</span>`;
}
function openingCutoffs(o) {
  const parts = [];
  if (o.entry_until) parts.push(`Last admission: ${budapestTime(o.entry_until)}.`);
  else if (o.orders_until) parts.push(`Orders accepted until ${budapestTime(o.orders_until)}.`);
  else if (o.until) parts.push(`${o.window_end_reason === 'unconfirmed_next_day' ? 'Hours confirmed until' : 'Operating window ends'} ${budapestTime(o.until)}.`);
  if (o.usable_until && (o.entry_until || o.orders_until)) parts.push(`Usable visitor hours until ${budapestTime(o.usable_until)}.`);
  return parts.join(' ');
}
function openingDetails(a) {
  const o = a.opening;
  if (!o) return '';
  const phase = openingPhase(a);
  if (phase !== 'fresh') return `<div class="opening-details" data-opening-activity="${esc(a.id)}"><p><strong>Current opening status</strong></p><div class="tags">${openingBadges(a)}</div><div class="opening-update" role="status"><p>${esc(openingUpdateMessage(phase))} Opening and finish-time checks are unavailable until an update succeeds.</p><button class="text-button" data-retry-opening ${phase === 'checking' ? 'disabled' : ''}>Try again</button></div><p class="opening-source">Last successful calculation: ${esc(budapestTime(o.as_of))}.${o.source_url ? ` ${external(o.source_url,'Published hours','source-link')} · checked ${esc(o.hours_checked_on)}.` : ''} Confirm current hours and availability with the venue.</p></div>`;
  const v = a.visit_now;
  const visit = o.state === 'open' && v ? `<div class="visit-now-summary"><p><strong>If starting immediately</strong></p><div class="tags">${currentVisitBadge(a)}</div>${v.finishes_at ? `<p>Full session would finish ${esc(budapestTime(v.finishes_at))}.</p>` : ''}<p>${esc(v.reason)}</p>${o.usage_note ? `<p>${esc(o.usage_note)}</p>` : ''}<p>Travel, preparation and waiting time are not included.</p></div>` : '';
  return `<div class="opening-details" data-opening-activity="${esc(a.id)}"><p><strong>Right now · Budapest time</strong></p><div class="tags">${openingBadges(a)}</div><p>${esc(o.reason)}</p><p>${esc(openingCutoffs(o))}</p>${o.note ? `<p>${esc(o.note)}</p>` : ''}${visit}<p class="opening-source">Calculated ${esc(budapestTime(o.as_of))}.${o.source_url ? ` ${external(o.source_url,'Published hours','source-link')} · checked ${esc(o.hours_checked_on)}.` : ''} Opening hours do not confirm available appointments or tickets.</p></div>`;
}
function activityDetailUrl(id) {
  const limit = $('[name=max_minutes]').value;
  return `/api/activities/${encodeURIComponent(id)}${limit ? `?max_minutes=${encodeURIComponent(limit)}` : ''}`;
}
function art(a, extra = '') {
  const symbol = artIds[a.category] || a.category;
  return `<div class="card-art art-${esc(a.category)} ${extra}"><svg viewBox="0 0 320 230" aria-hidden="true"><use href="/static/art.svg#${esc(symbol)}"></use></svg></div>`;
}
function safeLink(url) {
  try { const parsed = new URL(url); return parsed.protocol === 'https:' ? esc(parsed.href) : ''; }
  catch { return ''; }
}
function external(url, label, classes = 'button outline') {
  const href = safeLink(url);
  return href ? `<a class="${classes}" href="${href}" target="_blank" rel="noopener noreferrer">${esc(label)}</a>` : '';
}
function bookingLabel(a) {
  if (a.catalogue_active === false) return 'Check with the venue';
  return a.booking_label || (a.booking_url ? 'Booking & details' : 'Website & details');
}
function providerChecks(a) {
  const fields = {description:'What to expect', identity:'Branch', prices:'Price', price_context:'Pricing notes', duration:'Duration', hours:'Hours', booking:'Booking route', access:'Visitor conditions', student_offer:'Student offer', language:'Language', menu:'Menu'};
  return (a.provider_sources || []).map(source => external(source.url,source.fields.map(field => fields[field] || field).join(' · '),'source-link')).join('; ');
}
function directoryCredit(a) {
  const contributors = (a.source_records || []).map(s => s.dataset).filter((v,i,all) => all.indexOf(v) === i).map(esc).join(', ');
  return `Place identity: ${external('https://docs.overturemaps.org/guides/places/','Overture Maps','source-link')}, release ${esc(a.data_release)}. Contributors: ${contributors}. <a class="source-link" href="/static/licenses/CDLA-Permissive-2.0.txt" target="_blank" rel="noopener noreferrer">CDLA-Permissive-2.0 licence</a>.`;
}
function dateLabel(date) {
  return new Intl.DateTimeFormat('en-GB', {weekday:'long', day:'numeric', month:'long'}).format(new Date(`${date}T12:00:00`));
}
function applyServerDate(instant, refreshResults = true) {
  const today = budapestDay(instant);
  if (!today || !state.config || today < state.config.today) return false;
  const previous = state.config.today;
  state.config.today = today;
  $('#local-date').textContent = `${dateLabel(today)} · Budapest time`;
  const planDay = $('#create-plan-form [name=day]');
  planDay.min = today;
  if (!planDayEdited && (!planDay.value || planDay.value === previous)) planDay.value = today;
  const visited = $('#review-form [name=visited_on]');
  if (visited) visited.max = today;
  if (today !== previous && refreshResults && !$('#explore-view').hidden && !controller) loadActivities(true);
  return true;
}
async function refreshCalendarDate() {
  if (!state.config) return false;
  if (calendarRequest) return calendarRequest;
  calendarRequest = (async () => {
    try {
      const clock = await currentDateApi('/api/clock');
      if (!applyServerDate(clock.as_of)) throw new Error('Unconfirmed calendar date');
      return true;
    } catch {
      $('#local-date').textContent = `Last confirmed date: ${dateLabel(state.config.today)} · Budapest time`;
      return false;
    } finally {calendarRequest = null;}
  })();
  return calendarRequest;
}
function isPreviousDay(instant) {
  const day = budapestDay(instant);
  return day && day < state.config.today;
}
function eventLabel(a) {
  if (!a.starts_at) return '';
  const label = new Intl.DateTimeFormat('en-GB', {timeZone:'Europe/Budapest', day:'numeric', month:'short', hour:'2-digit', minute:'2-digit'}).format(new Date(a.starts_at));
  return `${label}${a.time_kind === 'doors' ? ' · doors' : ''}`;
}
function renderActivityGrid() {
  const expanded = new Set($$('#activity-grid .recommendation-details[open]').map(element => element.closest('[data-activity-id]').dataset.activityId));
  $('#activity-grid').innerHTML = state.items.slice(0,state.visible).map(card).join('');
  $$('#activity-grid [data-activity-id]').forEach(element => {
    if (expanded.has(element.dataset.activityId)) $('.recommendation-details',element).open = true;
  });
  $('#load-more-wrap').hidden = state.items.length <= state.visible;
  $('#showing-count').textContent = `${Math.min(state.visible,state.items.length)} of ${state.items.length} ideas shown`;
}
function renderListOpeningBadges() {
  const items = new Map(state.items.map(a => [a.id, a]));
  $$('#activity-grid [data-activity-id]').forEach(element => {
    const tags = $('.opening-tags', element);
    const activity = items.get(element.dataset.activityId);
    if (tags && activity) tags.innerHTML = openingBadges(activity) + currentVisitBadge(activity);
    if (activity) {
      const reasons = visibleRecommendationReasons(activity);
      $('.recommendation-summary',element).textContent = reasons[0];
      $('[data-recommendation-basis]',element).innerHTML = reasons.map(reason => `<li>${esc(reason)}</li>`).join('');
    }
  });
}
$('#load-more').addEventListener('click', () => {state.visible += 12; renderActivityGrid();});
function closeDialog(dialog) { dialog.close(); }
$$('[data-close]').forEach(button => button.addEventListener('click', () => closeDialog(button.closest('dialog'))));
$$('dialog').forEach(dialog => dialog.addEventListener('click', event => {
  if (event.target === dialog) { const box = dialog.getBoundingClientRect(); if (event.clientX < box.left || event.clientX > box.right || event.clientY < box.top || event.clientY > box.bottom) dialog.close(); }
}));

function renderCategories() {
  $('#categories').innerHTML = [['','Everything'], ...Object.entries(state.config.categories)].map(([key, label]) => `<button class="category-button ${state.category === key ? 'active' : ''}" data-category="${esc(key)}" aria-pressed="${state.category === key}">${esc(label)}</button>`).join('');
}
$('#categories').addEventListener('click', event => {
  const button = event.target.closest('[data-category]'); if (!button) return;
  state.category = button.dataset.category; renderCategories(); loadActivities();
});
function setPlanningWindow(when) {
  state.when = when;
  if (when !== 'today') $('#open-now').checked = false;
  $$('[data-when]').forEach(other => { const active = other.dataset.when === when; other.classList.toggle('active', active); other.setAttribute('aria-pressed', active); });
}
$$('[data-when]').forEach(button => button.addEventListener('click', () => {
  setPlanningWindow(button.dataset.when);
  loadActivities();
}));
$('#open-now').addEventListener('change', () => {
  if ($('#open-now').checked) setPlanningWindow('today');
  loadActivities();
});

function card(a) {
  const recommendation = visibleRecommendationReasons(a);
  const tags = [...(a.features || []).slice(0,2).map(tag => labels[tag])];
  tags.push(visitTimeLabel(a));
  if (a.source_kind === 'open_data') tags.push('Basic listing');
  if (a.source_kind === 'provider_enriched') tags.push('Venue details added');
  return `<article class="activity-card" data-activity-id="${esc(a.id)}">
    <button class="card-art art-${esc(a.category)}" data-detail="${esc(a.id)}" aria-label="View ${esc(a.title)}"><svg viewBox="0 0 320 230" aria-hidden="true"><use href="/static/art.svg#${esc(artIds[a.category] || a.category)}"></use></svg><span class="card-category">${esc(state.config.categories[a.category])}</span></button>
    <div class="card-body"><p class="venue-line">${esc(a.venue.name)}</p>${a.starts_at ? `<p class="event-when">${esc(eventLabel(a))}</p>` : ''}<button class="card-title" data-detail="${esc(a.id)}">${esc(a.title)}</button><p class="card-description">${esc(a.description)}</p>${state.when === 'today' ? `<div class="tags opening-tags">${openingBadges(a)}${currentVisitBadge(a)}</div>` : ''}${a.availability_note ? `<p class="availability-note">${esc(a.availability_note)}</p>` : ''}<div class="tags">${tags.map(t => `<span class="tag">${esc(t)}</span>`).join('')}${a.student_discount ? `<span class="tag" data-student-offer ${commercialState(a,'student_offer') !== 'current' ? 'hidden' : ''}>Student offer · conditions apply</span>` : ''}${a.kind === 'programme' ? '<span class="tag">Choose an event date</span>' : ''}</div><p class="match-reason recommendation-summary">${esc(recommendation[0])}</p><details class="recommendation-details"><summary>Why this idea?</summary><ul data-recommendation-basis>${recommendation.map(reason => `<li>${esc(reason)}</li>`).join('')}</ul></details></div>
    <div class="card-bottom"><span class="price ${a.price_low == null ? 'unknown' : ''}">${esc(price(a))}</span><button data-detail="${esc(a.id)}">Take a look</button></div></article>`;
}

function renderListUpdate() {
  const phase = listSnapshot?.phase || 'checking';
  const stale = phase !== 'fresh';
  $('#opening-update').hidden = !stale;
  $('#retry-opening').disabled = phase === 'checking';
  if (!stale) {
    $('#results-title').textContent = listCopy.title;
    $('#results-note').textContent = listCopy.note;
    return;
  }
  const reference = state.items.length ? ' Previous matches are shown for reference.' : '';
  $('#opening-update-message').textContent = (state.when === 'today' ? openingUpdateMessage(phase) : phase === 'checking' ? 'Updating ideas…' : 'Ideas could not be updated. Please try again.') + reference;
  $('#results-title').textContent = state.items.length ? `${state.items.length} previously loaded ideas` : phase === 'checking' ? 'Finding your next thing…' : 'Could not load ideas';
  $('#results-note').textContent = state.items.length ? `Previous results for your selected filters.${state.when === 'today' ? ' Current opening and finish-time checks are unavailable.' : ''}` : 'Check current hours and booking availability with each venue.';
}
function renderDetailOpening() {
  const current = $('.opening-details');
  if (detailActivity && $('#detail-dialog').open && current?.dataset.openingActivity === detailActivity.id) current.outerHTML = openingDetails(detailActivity);
}

async function loadActivities(keepVisible = false) {
  if (!state.config) return;
  controller?.abort(); const requestController = new AbortController(); controller = requestController;
  const data = new FormData($('#filters'));
  const params = new URLSearchParams({when:state.when});
  if (state.category) params.set('category', state.category);
  for (const [key, value] of data) if (value) params.append(key, value);
  const query = params.toString();
  const sameQuery = query === listQuery;
  if (!sameQuery) {
    state.items = []; listCopy = null; listQuery = query;
    $('#empty-state').hidden = true;
    $('#event-links').innerHTML = '';
  }
  listSnapshot = {phase:'checking', startedAt:performance.now()};
  state.items.forEach(a => a._openingSnapshot = listSnapshot);
  $('#empty-state').hidden = true;
  if (sameQuery) renderListOpeningBadges();
  else renderActivityGrid();
  renderListUpdate();
  $('#activity-grid').setAttribute('aria-busy','true');
  try {
    const result = await currentDateApi(`/api/activities?${params}`, requestController.signal);
    if (controller !== requestController) return;
    applyServerDate(result.as_of, false);
    $('#ranking-description').textContent = result.ordering?.description || 'All selected filters are required. Check each idea’s published details.';
    listSnapshot.phase = navigator.onLine ? 'fresh' : 'offline';
    state.items = result.items.map(a => anchorCommercialClock({...a, _openingSnapshot:listSnapshot},listSnapshot.startedAt)); if (!keepVisible) state.visible = 12; renderActivityGrid();
    $('#results-title').textContent = `${result.total} ${result.total === 1 ? 'idea' : 'ideas'} for your ${state.when === 'weekend' ? 'weekend' : state.when === 'today' ? 'day' : 'next day off'}`;
    const notes = [`Across ${result.places} places and providers. Check current hours and booking availability.`];
    if (result.open_now) notes.push(`Open now uses published hours in Budapest, calculated ${budapestTime(result.as_of)}. Known fixed sessions must also fit before the usable window ends if started immediately. Appointments and tickets still need confirmation.`);
    if (result.basic_places) notes.push(`Includes ${result.basic_places} basic listing${result.basic_places === 1 ? '' : 's'} with details to confirm.`);
    if (data.get('mood') || data.get('company') || data.get('energy') || data.get('feature')) notes.push('Places without matching suitability tags are hidden.');
    if (data.get('max_price')) notes.push('Only published upper prices within your budget are shown; check extras.');
    if (data.get('max_minutes')) notes.push('Includes sessions within your time limit and visits where you choose how long to stay. Check closing times and ticket limits. Travel and waiting time are extra.');
    if (data.get('student')) notes.push('Student offers may require a specific card or group size.');
    $('#results-note').textContent = notes.join(' ');
    listCopy = {title:$('#results-title').textContent, note:$('#results-note').textContent};
    renderListUpdate();
    $('#empty-state').hidden = result.total > 0;
    $('#empty-title').textContent = "Let's try a different angle.";
    $('#empty-reset').hidden = false;
    const events = ['events','nightlife'].includes(state.category);
    $('#empty-message').textContent = result.open_now ? 'No matching activities have confirmed published opening hours for this moment. Try fewer filters or turn off Open now to include unconfirmed schedules.' : events ? 'No dated events have been confirmed in this collection for your chosen window. Check the official programme or explore another category.' : 'No matches in this collection. Try Any day, another category or fewer filters. Budget limits exclude unconfirmed prices; time limits exclude sessions with unconfirmed lengths.';
    $('#event-links').innerHTML = events ? state.config.event_sources.map(source => external(source.url, source.name, 'text-link')).join('') : '';
  } catch (error) {
    if (controller !== requestController || error.name === 'AbortError') return;
    listSnapshot.phase = navigator.onLine ? 'failed' : 'offline';
    renderListOpeningBadges(); renderListUpdate();
    $('#empty-state').hidden = state.items.length > 0;
    $('#empty-title').textContent = 'Ideas are temporarily unavailable.';
    $('#empty-message').textContent = 'Your filters are unchanged. Use Try again above to reload the results.';
    $('#empty-reset').hidden = true;
    $('#event-links').innerHTML = '';
  } finally {
    if (controller === requestController) {
      controller = null;
      $('#activity-grid').setAttribute('aria-busy','false');
    }
  }
}

$('#filters').addEventListener('submit', event => {event.preventDefault(); loadActivities();});
$('#filters').addEventListener('change', event => {if (event.target.name !== 'q') loadActivities();});
let searchTimer;
$('#filters [name=q]').addEventListener('input', () => {clearTimeout(searchTimer); searchTimer = setTimeout(loadActivities,300);});
function resetFilters() { $('#filters').reset(); state.category = ''; renderCategories(); loadActivities(); }
$('#clear-filters').addEventListener('click', resetFilters);
$('#empty-reset').addEventListener('click', resetFilters);
async function refreshDetailOpening() {
  if (!detailActivity || !$('#detail-dialog').open || detailController) return;
  const requestController = new AbortController(); detailController = requestController;
  const version = ++detailVersion;
  const activity = detailActivity;
  const snapshot = {phase:'checking', startedAt:performance.now()};
  activity._openingSnapshot = snapshot; renderDetailOpening();
  try {
    const result = await currentDateApi(activityDetailUrl(activity.id), requestController.signal);
    if (version !== detailVersion || !$('#detail-dialog').open) return;
    applyServerDate(result.opening?.as_of);
    snapshot.phase = navigator.onLine ? 'fresh' : 'offline';
    detailActivity = anchorCommercialClock({...result, _openingSnapshot:snapshot},snapshot.startedAt);
    renderDetailOpening();
    renderCommercialViews();
  } catch (error) {
    if (version !== detailVersion || error.name === 'AbortError') return;
    snapshot.phase = navigator.onLine ? 'failed' : 'offline'; renderDetailOpening();
  } finally {if (detailController === requestController) detailController = null;}
}
async function refreshOpenNow() {
  if (document.hidden) return;
  if (!startupReady) {await start(); return;}
  const work = [refreshCalendarDate()];
  if (!$('#explore-view').hidden && !controller) work.push(loadActivities(true));
  if ($('#detail-dialog').open) work.push(refreshDetailOpening());
  await Promise.allSettled(work);
}
function expireOpeningChecks() {
  if (document.hidden) return;
  let expired = false;
  const activities = [...state.items, ...(detailActivity ? [detailActivity] : []), ...(!$('#plan-view').hidden ? (state.plan?.candidates || []).map(c => c.activity) : [])];
  for (const activity of activities) {
    if (!activity._commercialExpired && ['price','student_offer'].some(field => activity.provider_freshness?.[field]?.state === 'current' && commercialState(activity,field) !== 'current')) {
      activity._commercialExpired = true; expired = true;
    }
  }
  renderCommercialViews();
  if (state.when === 'today' && listSnapshot?.phase === 'fresh' && state.items.some(a => openingPhase(a) !== 'fresh')) {
    listSnapshot.phase = 'expired'; renderListOpeningBadges(); renderListUpdate(); expired = true;
  }
  if ($('#detail-dialog').open && detailActivity?._openingSnapshot.phase === 'fresh' && openingPhase(detailActivity) !== 'fresh') {
    detailActivity._openingSnapshot.phase = 'expired'; renderDetailOpening(); expired = true;
  }
  if (expired) refreshOpenNow();
}
setInterval(refreshOpenNow, OPENING_REFRESH_MS);
setInterval(expireOpeningChecks, 1000);
document.addEventListener('visibilitychange', () => {if (!document.hidden) refreshOpenNow();});
window.addEventListener('online', refreshOpenNow);
window.addEventListener('pageshow', event => {if (event.persisted) refreshOpenNow();});
window.addEventListener('offline', () => {
  controller?.abort(); controller = null;
  detailController?.abort(); detailController = null; detailVersion++;
  if (listSnapshot) {listSnapshot.phase = 'offline'; renderListOpeningBadges(); renderListUpdate();}
  $('#activity-grid').setAttribute('aria-busy','false');
  if (detailActivity) {detailActivity._openingSnapshot.phase = 'offline'; renderDetailOpening();}
  if (state.config) $('#local-date').textContent = `Last confirmed date: ${dateLabel(state.config.today)} · Budapest time`;
});
$('#retry-opening').addEventListener('click', () => loadActivities(true));
document.addEventListener('click', event => {if (event.target.closest('[data-retry-opening]')) refreshDetailOpening();});
$('#detail-dialog').addEventListener('close', () => {
  detailController?.abort(); detailController = null; detailVersion++; detailActivity = null;
});

function reviewMarkup(r) {
  return `<article class="review" data-review-id="${esc(r.id)}"><div class="review-meta"><strong>${esc(r.nickname)}</strong> · ${esc(r.rating)}/5 · Visited ${esc(r.visited_on)}</div><p>${esc(r.text)}</p></article>`;
}
function detailStillShowing(form) {return form && $('#detail-dialog').open && $('#review-form') === form;}
async function showDetail(id) {
  detailController?.abort();
  const requestController = new AbortController(); detailController = requestController;
  const version = ++detailVersion;
  const snapshot = {phase:'fresh', startedAt:performance.now()};
  try {
    const [a, reviews] = await Promise.all([currentDateApi(activityDetailUrl(id), requestController.signal), openingApi(`/api/activities/${encodeURIComponent(id)}/reviews`, requestController.signal)]);
    if (version !== detailVersion) return;
    applyServerDate(a.opening?.as_of);
    a._openingSnapshot = snapshot; detailActivity = anchorCommercialClock(a,snapshot.startedAt);
    const maps = `https://www.google.com/maps/search/?api=1&query=${encodeURIComponent(`${a.venue.name} ${a.venue.location}`)}`;
    const canSuggest = a.catalogue_active !== false && (!state.plan || state.plan.selected_candidate === null);
    const isBasic = a.source_kind === 'open_data';
    const sourceNote = isBasic
      ? `${external(a.source_url,a.source_label,'source-link')} · release ${esc(a.data_release)}, imported ${esc(a.data_imported_on)}. Contributors: ${(a.source_records || []).map(s => s.dataset).filter((v,i,all) => all.indexOf(v) === i).map(esc).join(', ')}. <a class="source-link" href="/static/licenses/CDLA-Permissive-2.0.txt" target="_blank" rel="noopener noreferrer">CDLA-Permissive-2.0 licence</a>. The listing supplies basic identity and location; hours, prices, access and suitability have not been confirmed with the venue.`
      : a.source_kind === 'provider_enriched'
      ? `Checked on official pages, ${esc(a.source_checked_on)}: ${providerChecks(a)}. Only the listed information was checked; prices and schedules can change, and the venue confirms availability. ${esc(a.editorial_note)} ${directoryCredit(a)}`
      : `${external(a.source_url,'Official source','source-link')}${(a.source_urls || []).map((url,i) => ` · ${external(url,`Additional source ${i+1}`,'source-link')}`).join('')} · checked ${esc(a.source_checked_on)}. Prices and schedules can change. Activity and mood tags are our suggestions; live availability is confirmed by the venue.`;
    $('#detail-content').innerHTML = `<div class="detail-top"><div class="detail-art art-${esc(a.category)}"><svg viewBox="0 0 320 230" aria-hidden="true"><use href="/static/art.svg#${esc(artIds[a.category] || a.category)}"></use></svg></div><div class="detail-intro"><p class="eyebrow">${esc(state.config.categories[a.category])} · ${esc(a.venue.name)}</p><h2 id="detail-title">${esc(a.title)}</h2><p>${esc(a.description)}</p><p><strong>Worth knowing:</strong> ${esc(a.highlight)}</p><div class="tags">${(a.moods || []).map(tag => `<span class="tag">${esc(labels[tag])}</span>`).join('')}</div></div></div>
    <div class="detail-meta"><div><h3>Published price</h3><p class="meta-price">${esc(price(a))}</p><p class="detail-price-note">${esc(a.price_note)}</p></div><div><h3>Time & opening hours</h3>${a.starts_at ? `<p class="event-when">${esc(eventLabel(a))}</p><p>${esc(a.event_time_note || '')}</p>` : ''}${visitTimeDetails(a)}<p>${esc(a.hours)}</p>${a.availability_note ? `<p class="availability-note">${esc(a.availability_note)}</p>` : ''}${openingDetails(a)}</div><div><h3>Location</h3><p>${esc(a.venue.location)}</p></div><div><h3>Language & student offers</h3><p>${esc(a.language)}</p><p class="detail-student-note">${esc(a.student_note)}</p></div></div>
    ${a.requirements?.length ? `<ul class="requirements">${a.requirements.map(r => `<li>${esc(r)}</li>`).join('')}</ul>` : ''}
    <div class="detail-actions">${external(a.booking_url || a.venue.website,bookingLabel(a),'button dark')}${external(a.whatsapp_url,'WhatsApp')}${external(a.instagram_url,'Instagram')}${external(maps,'Directions')}${canSuggest ? `<button class="button lime" data-suggest="${esc(a.id)}">${state.planId ? 'Suggest to our plan' : 'Suggest to friends'}</button>` : ''}<button class="button outline" data-share="${esc(a.id)}">Share</button></div>
    <p class="source-note">${esc(a.catalogue_note || '')} ${sourceNote} Illustrations represent activities, not venue photographs.</p>
    <section class="reviews-section"><h3>Been here? Tell us how it was.</h3><div id="activity-reviews">${reviews.length ? reviews.map(reviewMarkup).join('') : '<p class="review-empty">No reviews yet. Share your own experience — there are no imported reviews here.</p>'}</div>
    <form class="review-form" id="review-form" data-activity="${esc(a.id)}"><label>Your nickname<input name="nickname" required minlength="2" maxlength="40"></label><label>Your rating<select name="rating"><option value="5">5 / 5</option><option value="4">4 / 5</option><option value="3">3 / 5</option><option value="2">2 / 5</option><option value="1">1 / 5</option></select></label><label>Visited on<input name="visited_on" type="date" required max="${esc(state.config.today)}"></label><label class="wide">Your experience<textarea name="text" required minlength="10" maxlength="1500" placeholder="What did you do? What should someone know before going?"></textarea></label><button class="button dark small" type="submit">Publish my review</button><p class="form-error" role="alert"></p></form></section>`;
    if (!$('#detail-dialog').open) $('#detail-dialog').showModal();
    $('#detail-dialog').scrollTop = 0;
    $('#review-form').addEventListener('submit', async event => {
      event.preventDefault(); const form = event.currentTarget;
      await formAction(form, async () => {
        if (!await refreshCalendarDate()) throw new Error('Could not check today’s Budapest date. Please try again when connected.');
        if (!form.reportValidity()) return;
        const values = Object.fromEntries(new FormData(form)); values.rating = Number(values.rating);
        const saved = await send(`/api/activities/${encodeURIComponent(a.id)}/reviews`, values);
        if (detailStillShowing(form)) {
          const current = Object.fromEntries(new FormData(form)); current.rating = Number(current.rating);
          const unchanged = Object.keys(values).every(key => values[key] === current[key]);
          const displayed = [saved.review, ...reviews.filter(r => r.id !== saved.review.id)].slice(0,100);
          reviews.splice(0,reviews.length,...displayed);
          $('#activity-reviews').innerHTML = reviews.map(reviewMarkup).join('');
          if (unchanged) form.reset();
        }
        notify('Thanks — your submitted review is published.');
      });
    });
    $('#review-form [name=visited_on]').addEventListener('focus', refreshCalendarDate);
  } catch (error) {if (version === detailVersion && error.name !== 'AbortError') notify(error.message);}
  finally {if (detailController === requestController) detailController = null;}
}

async function copyLink(url) {
  try { await navigator.clipboard.writeText(url); notify('Link copied. Send it to your friends.'); }
  catch {
    $('#share-url').value = url;
    if (!$('#share-dialog').open) $('#share-dialog').showModal();
    selectShareLink();
  }
}
function selectShareLink() {const input = $('#share-url'); input.focus(); input.select();}
$('#select-share-link').addEventListener('click',selectShareLink);
document.addEventListener('click', async event => {
  const detail = event.target.closest('[data-detail]');
  if (detail) {showDetail(detail.dataset.detail); return;}
  const share = event.target.closest('[data-share]');
  if (share) {await copyLink(`${location.origin}/?activity=${encodeURIComponent(share.dataset.share)}`); return;}
  const suggest = event.target.closest('[data-suggest]');
  if (suggest) {
    if (!state.planId) {$('#detail-dialog').close(); openCreatePlan(suggest.dataset.suggest); return;}
    const sourceForm = $('#review-form');
    try {
      suggest.disabled = true;
      await mutatePlan(`/api/plans/${state.planId}/candidates`, {activity_id:suggest.dataset.suggest});
      notify('Added to your plan. Let the voting begin.');
      if (detailStillShowing(sourceForm)) $('#detail-dialog').close();
    } catch (error) {notify(error.message);} finally {suggest.disabled = state.plan?.selected_candidate != null;}
  }
});

function openCreatePlan(activityId = null) {
  if (!startupReady) {notify('The app is still connecting. Please try again when it is ready.'); return;}
  state.pendingActivity = activityId;
  planDayEdited = false;
  const form = $('#create-plan-form'); form.reset(); $('.form-error',form).textContent = '';
  $('[name=day]',form).min = state.config.today;
  $('[name=day]',form).value = state.config.today;
  const eventDay = detailActivity?.id === activityId && detailActivity.kind === 'event' ? budapestDay(detailActivity.starts_at) : null;
  if (eventDay && eventDay >= state.config.today) {$('[name=day]',form).value = eventDay; planDayEdited = true;}
  $('#plan-dialog').showModal();
  refreshCalendarDate();
}
$('#create-plan-form [name=day]').addEventListener('input', () => {planDayEdited = true;});
$('#create-plan-form [name=day]').addEventListener('change', () => {planDayEdited = true;});
$('#create-plan-form [name=day]').addEventListener('focus', refreshCalendarDate);
$('#new-plan').addEventListener('click', () => openCreatePlan());
$('#friends-cta').addEventListener('click', () => openCreatePlan());

async function formAction(form, action) {
  const button = $('[type=submit]', form); button.disabled = true; $('.form-error',form).textContent = '';
  try { await action(); }
  catch (error) {
    const dialog = form.closest('dialog');
    if (form.isConnected && (!dialog || dialog.open)) $('.form-error',form).textContent = error.message;
    else notify(`Could not confirm your submission. ${error.message}`);
  }
  finally { button.disabled = false; }
}
$('#create-plan-form').addEventListener('submit', async event => {
  event.preventDefault(); const form = event.currentTarget;
  await formAction(form, async () => {
    if (!await refreshCalendarDate()) throw new Error('Could not check today’s Budapest date. Please try again when connected.');
    if (!form.reportValidity()) return;
    const values = Object.fromEntries(new FormData(form));
    values.use_today = !planDayEdited && values.day === state.config.today;
    if (state.pendingActivity) values.activity_id = state.pendingActivity;
    const plan = await send('/api/plans', values);
    location.assign(`/plan/${plan.id}`);
  });
});
$('#join-plan-form').addEventListener('submit', async event => {
  event.preventDefault(); const form = event.currentTarget;
  await formAction(form, async () => {
    await mutatePlan(`/api/plans/${state.planId}/join`,Object.fromEntries(new FormData(form)));
    $('#join-dialog').close(); renderPlan();
  });
});

function renderPlan() {
  const p = state.plan;
  p.candidates.forEach(c => anchorCommercialClock(c.activity));
  const decided = p.selected_candidate !== null;
  $('#plan-content').innerHTML = `${planRefreshError ? `<p id="plan-refresh-error" role="alert">${esc(planRefreshError)}</p>` : ''}<div class="plan-head"><p class="eyebrow">GOOD PLANS START WITH A FEW IDEAS.</p><h1 id="plan-title">${esc(p.title)}</h1><p>${esc(dateLabel(p.day))} · ${p.participants.length} ${p.participants.length === 1 ? 'person' : 'people'} in the plan</p><div class="people">${p.participants.map(n => `<span class="person">${esc(n)}</span>`).join('')}</div><div class="plan-tools"><button class="button dark" id="copy-plan">Copy invite link</button>${!p.me && !decided ? '<button class="button lime" id="join-plan">Join the plan</button>' : ''}${p.me && !decided ? `<a class="button lime" href="/?plan=${esc(p.id)}">Suggest an idea</a>` : ''}<button class="button outline" id="refresh-plan">Refresh votes</button></div><p class="plan-note">${decided ? 'The plan is decided. Check details and book directly with the venue.' : 'Everyone can suggest and vote. You can change your vote; the creator confirms the final choice.'} Anyone with your invitation link can view the plan. Participation is remembered in this browser.</p></div>
    ${!p.candidates.length ? `<div class="empty-state"><span class="empty-star" aria-hidden="true">✳</span><h3>Your first idea goes here.</h3><p>Browse Budapest and suggest something you'd love to do.</p>${p.me ? `<a class="button dark" href="/?plan=${esc(p.id)}">Find an idea</a>` : ''}</div>` : `<div class="candidate-grid">${p.candidates.map(c => {
      const a = c.activity; const selected = p.selected_candidate === c.id;
      return `<article class="candidate ${selected ? 'selected' : ''}" data-candidate-activity="${esc(a.id)}">${art(a)}<div class="candidate-inner">${selected ? '<span class="decided-label">IT’S A PLAN.</span>' : ''}<p class="venue-line">Suggested by ${esc(c.proposed_by)} · ${esc(a.venue.name)}</p><h3>${esc(a.title)}</h3><span class="price ${a.price_low == null ? 'unknown' : ''}">${esc(price(a))}</span>${c.day_problem ? `<p class="plan-date-warning">${esc(c.day_problem)}</p>` : ''}<div class="vote-options">${[['yes','I’m in'],['maybe','Maybe'],['no','Not for me']].map(([value,label]) => `<button class="vote-button" data-vote="${c.id}" data-choice="${value}" aria-pressed="${c.my_vote === value}" ${!p.me || decided || planMutationPending ? 'disabled' : ''}>${label} · ${c.votes[value]}</button>`).join('')}</div><div class="candidate-actions"><button class="text-button" data-detail="${esc(a.id)}">View details</button>${p.me?.is_host && !decided ? `<button class="button dark small" data-pick="${c.id}" ${c.day_problem || planMutationPending ? 'disabled' : ''}>Make it the plan</button>` : selected ? external(a.booking_url || a.venue.website,bookingLabel(a),'button dark small') : ''}</div></div></article>`;
    }).join('')}</div>`}`;
  $('#copy-plan').addEventListener('click', () => copyLink(`${location.origin}/plan/${p.id}`));
  $('#refresh-plan').addEventListener('click', loadPlan);
  $('#join-plan')?.addEventListener('click', () => $('#join-dialog').showModal());
  $$('[data-vote]').forEach(button => button.addEventListener('click', async () => {
    button.disabled = true;
    try {await mutatePlan(`/api/plans/${p.id}/candidates/${button.dataset.vote}/vote`,{choice:button.dataset.choice},'PUT');}
    catch (error) {notify(error.message);}
  }));
  $$('[data-pick]').forEach(button => button.addEventListener('click', async () => {
    button.disabled = true;
    try {await mutatePlan(`/api/plans/${p.id}/pick`,{candidate_id:Number(button.dataset.pick)});}
    catch (error) {notify(error.message);}
  }));
}
async function mutatePlan(path, body, method = 'POST') {
  if (planMutationPending) throw new Error('Please wait for your previous change.');
  planMutationPending = true; ++planReadVersion;
  $$('#plan-content [data-vote], #plan-content [data-pick], [data-suggest], #join-plan').forEach(b => b.disabled = true);
  try {state.plan = await send(path,body,method); planRefreshError = ''; return state.plan;}
  catch (error) {
    if (error.status === 409 || error.status === 422) {
      await loadPlan(true);
      if (state.plan?.selected_candidate != null && $('#join-dialog').open) $('#join-dialog').close();
    }
    throw error;
  } finally {
    planMutationPending = false;
    if (state.plan && !$('#plan-view').hidden) renderPlan();
    $$('[data-suggest]').forEach(b => b.disabled = state.plan?.selected_candidate != null);
  }
}
async function loadPlan(allowDuringMutation = false) {
  if (planMutationPending && allowDuringMutation !== true) return;
  const version = ++planReadVersion;
  try {
    const plan = await openingApi(`/api/plans/${state.planId}`);
    if (version !== planReadVersion) return;
    state.plan = plan;
    planRefreshError = '';
    if (!$('#plan-view').hidden) renderPlan();
  } catch (error) {
    if (version !== planReadVersion) return;
    if (state.plan) {
      planRefreshError = `Could not refresh. These are the last loaded votes. ${error.message} Use Refresh votes to retry.`;
      if (!$('#plan-view').hidden) renderPlan();
    } else {
      $('#plan-content').innerHTML = `<div class="empty-state"><h1>${error.status === 404 ? "Let's check that link." : 'Could not load your plan.'}</h1><p>${esc(error.message)}</p><button class="button dark" id="retry-plan">Try again</button><a class="button outline" href="/">Explore Budapest</a></div>`;
      $('#retry-plan').addEventListener('click', () => loadPlan());
    }
  }
}

function renderStartup(phase, message = '') {
  const ready = phase === 'ready';
  $('#startup-update').hidden = ready;
  $('#startup-update').setAttribute('aria-busy', String(phase === 'loading'));
  $('#startup-message').textContent = phase === 'loading' ? 'Connecting to Offscript…' : `Could not connect to Offscript. ${message}`;
  $('#retry-startup').hidden = phase !== 'failed';
  $('#retry-startup').disabled = phase === 'loading';
  for (const selector of ['#new-plan','#friends-cta']) $(selector).disabled = !ready;
  for (const selector of ['#explore-view','#plan-view']) $(selector).inert = !ready;
}
async function bootstrap() {
  renderStartup('loading');
  try {
    state.config = await openingApi('/api/config',undefined,'Connection timed out. Please try again.');
    applyServerDate(state.config.as_of, false);
    $('#app-version').textContent = `Budapest · v${state.config.version}`;
    const planMatch = location.pathname.match(/^\/plan\/([A-Za-z0-9_-]+)$/);
    state.planId = planMatch?.[1] || new URLSearchParams(location.search).get('plan');
    if (state.planId && !/^[A-Za-z0-9_-]{24}$/.test(state.planId)) state.planId = null;
    state.plan = null;
    $('#active-plan-banner').hidden = true;
    $('#back-to-plan').hidden = !state.planId;
    if (state.planId) {$('#back-to-plan').hidden = false; $('#back-to-plan').href = `/plan/${state.planId}`;}
    if (planMatch) {
      $('#explore-view').hidden = true; $('#plan-view').hidden = false; await loadPlan();
    } else {
      renderCategories(); await loadActivities();
      if (state.planId) {
        try {
          state.plan = await openingApi(`/api/plans/${state.planId}`);
          $('#active-plan-banner').hidden = false;
          $('#active-plan-banner').innerHTML = `<span>Picking ideas for <strong>${esc(state.plan.title)}</strong></span><a href="/plan/${esc(state.planId)}">Back to our plan</a>`;
        } catch (error) {
          if (error.status !== 404) throw error;
          notify('That plan link is no longer available.'); state.planId = null;
          $('#back-to-plan').hidden = true;
        }
      }
    }
    startupReady = true; renderStartup('ready');
    const detailId = new URLSearchParams(location.search).get('activity');
    if (detailId) await showDetail(detailId);
  } catch (error) {
    startupReady = false; renderStartup('failed',error.message);
    $('#results-title').textContent = 'Could not connect to the app';
    $('#results-note').textContent = error.message;
    notify(error.message);
  }
}
async function start() {
  if (startupReady) return;
  if (startRequest) return startRequest;
  startRequest = bootstrap();
  try {await startRequest;} finally {startRequest = null;}
}
$('#retry-startup').addEventListener('click',start);
start();
