const $ = (id) => document.getElementById(id);
const PAGE_SIZE = 24;
const state = {
  sessions: [], session: null, rows: [], likes: {}, selected: 0,
  likedOnly: false, sort: 'time', page: 0, ms: 33, mode: 'saved', request: 0,
  viewMode: 'single', binIndex: 29, binData: null, binKey: null,
  binCache: new Map(), binRequest: 0, binsCollapsed: false,
  account: null, likerKey: null, likerRequest: 0,
};
const number = new Intl.NumberFormat('ko-KR');
const timeFormat = new Intl.DateTimeFormat('ko-KR', {hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false});
let toastTimer;
let eventTimer;
let likeQueue = Promise.resolve();

function toast(message) {
  const element = $('toast');
  element.textContent = message;
  element.classList.add('visible');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => element.classList.remove('visible'), 2600);
}

async function getJSON(path, options) {
  const response = await fetch(path, options);
  if (!response.ok) throw new Error(`HTTP ${response.status}`);
  return response.json();
}

function timestamp(us) {
  return timeFormat.format(new Date(Math.floor(us / 1000)));
}

function sessionLabel(id) {
  const date = id.match(/(\d{8})_(\d{6})$/);
  if (!date) return id;
  return `${date[1].slice(4, 6)}.${date[1].slice(6, 8)}  ${date[2].slice(0, 2)}:${date[2].slice(2, 4)}:${date[2].slice(4, 6)}`;
}

function likeFor(index) {
  return state.likes[String(index)] || {count: 0, mine: false};
}

function renderAccount() {
  $('accountName').hidden = !state.account;
  $('accountName').textContent = state.account ? `${state.account.username}님` : '';
  $('authButton').textContent = state.account ? '로그아웃' : '로그인';
}

function openLogin() {
  $('authError').textContent = '';
  $('authPassword').value = '';
  $('authDialog').showModal();
  $('authUsername').focus();
}

async function loadLikeOwners(force = false) {
  if (!state.session) return;
  const key = `${state.session.id}/${state.selected}`;
  if (!force && key === state.likerKey) return;
  state.likerKey = key;
  const request = ++state.likerRequest;
  $('likeOwners').textContent = '좋아요를 확인하는 중…';
  try {
    const data = await getJSON(`/api/likes/${sessionPath(state.session.id)}/${state.selected}`);
    if (request !== state.likerRequest || key !== `${state.session?.id}/${state.selected}`) return;
    const names = data.users.join(', ');
    const anonymous = data.anonymous_count ? `기존 익명 ${number.format(data.anonymous_count)}명` : '';
    $('likeOwners').textContent = data.count ? `좋아요 누른 사람: ${[names, anonymous].filter(Boolean).join(' · ')}` : '아직 좋아요가 없습니다.';
  } catch (error) {
    if (request === state.likerRequest) $('likeOwners').textContent = '좋아요 정보를 불러오지 못했습니다.';
  }
}

async function submitLogin(event) {
  event.preventDefault();
  $('authSubmit').disabled = true;
  $('authError').textContent = '';
  try {
    const data = await getJSON('/api/auth/login', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({username: $('authUsername').value, password: $('authPassword').value}),
    });
    state.account = data.user;
    renderAccount();
    $('authDialog').close();
    $('authPassword').value = '';
    if (state.session) await loadSession(state.session.id, state.selected);
    state.likerKey = null;
    loadLikeOwners(true);
  } catch (error) {
    $('authError').textContent = '아이디 또는 비밀번호를 확인해 주세요.';
  } finally {
    $('authSubmit').disabled = false;
  }
}

async function logout() {
  try {
    await getJSON('/api/auth/logout', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: '{}'});
    state.account = null;
    renderAccount();
    if (state.session) await loadSession(state.session.id, state.selected);
  } catch (error) {
    toast('로그아웃하지 못했습니다. 다시 시도해 주세요.');
  }
}

function sessionPath(id) {
  return encodeURIComponent(id);
}

function imagePath(kind, index) {
  return `/api/image/${sessionPath(state.session.id)}/${kind}/${index}`;
}

function binKey() {
  return `${state.session.id}/${state.selected}`;
}

function binImagePath(index) {
  return `/api/bin/${sessionPath(state.session.id)}/${state.selected}/${index}?v=previous100ms`;
}

function offsetLabel(us) {
  const ms = us / 1000;
  return `${ms > 0 ? '+' : ''}${ms.toFixed(3)}`;
}

function filteredRows() {
  let rows = state.rows.filter((row) => !state.likedOnly || likeFor(row.index).count > 0);
  if (state.sort === 'likes') rows = [...rows].sort((a, b) => likeFor(b.index).count - likeFor(a.index).count || a.index - b.index);
  return rows;
}

function updateURL() {
  if (!state.session) return;
  const url = new URL(location.href);
  url.searchParams.set('session', state.session.id);
  url.searchParams.set('frame', String(state.selected));
  url.searchParams.set('view', state.viewMode);
  if (state.viewMode === 'bins') url.searchParams.set('bin', String(state.binIndex));
  else url.searchParams.delete('bin');
  if (state.viewMode === 'single' && state.mode === 'dynamic') {
    url.searchParams.set('seconds', (state.ms / 1000).toFixed(3));
  } else {
    url.searchParams.delete('seconds');
  }
  history.replaceState(null, '', url);
}

function renderBinSelection() {
  if (!state.binData || state.binKey !== binKey()) return;
  const selected = state.binData.bins[state.binIndex];
  if (!selected) return;
  $('eventImage').hidden = false;
  $('eventImage').src = binImagePath(state.binIndex);
  $('eventImage').alt = `선택한 ${state.binIndex + 1}번 이벤트 bin`;
  $('eventModeLabel').textContent = `BIN ${String(state.binIndex + 1).padStart(2, '0')} / 30`;
  $('eventFoot').textContent = `${offsetLabel(selected.start_offset_us)} ~ ${offsetLabel(selected.end_offset_us)} ms · ${number.format(selected.event_count)}개`;
  $('binPrevButton').disabled = state.binIndex === 0;
  $('binNextButton').disabled = state.binIndex === 29;
  for (const button of $('binGrid').children) {
    const active = Number(button.dataset.bin) === state.binIndex;
    button.classList.toggle('selected', active);
    button.setAttribute('aria-pressed', String(active));
  }
  updateURL();
}

function renderBinGrid() {
  const data = state.binData;
  if (!data || state.binKey !== binKey()) return;
  $('binAlignment').textContent = data.center_source === 'frame_timestamp_fallback'
    ? '노출 정보 없음 · 프레임 타임스탬프 기준'
    : '노출 중앙 기준 · 프레임 시각 + 노출 시간의 절반';
  $('binStatus').textContent = '시간 범위는 기준 시각과의 차이입니다. 칸을 누르거나 큰 이미지 위 ←/→, 키보드 ↑/↓로 bin을 넘기세요.';
  const grid = $('binGrid');
  grid.replaceChildren();
  for (const bin of data.bins) {
    const button = document.createElement('button');
    button.type = 'button';
    button.className = 'bin-card';
    button.dataset.bin = String(bin.index);
    button.setAttribute('aria-label', `${bin.index + 1}번 bin, ${offsetLabel(bin.start_offset_us)}에서 ${offsetLabel(bin.end_offset_us)}밀리초, 이벤트 ${bin.event_count}개`);
    const header = document.createElement('div');
    header.className = 'bin-card-head';
    const numberLabel = document.createElement('strong');
    numberLabel.textContent = `BIN ${String(bin.index + 1).padStart(2, '0')}`;
    const countLabel = document.createElement('span');
    countLabel.textContent = `${number.format(bin.event_count)} ev`;
    header.append(numberLabel, countLabel);
    const image = document.createElement('img');
    image.src = binImagePath(bin.index);
    image.alt = '';
    image.loading = 'lazy';
    const interval = document.createElement('span');
    interval.className = 'bin-time';
    interval.textContent = `${offsetLabel(bin.start_offset_us)} ~ ${offsetLabel(bin.end_offset_us)} ms`;
    const polarity = document.createElement('span');
    polarity.className = 'bin-polarity';
    const onDot = document.createElement('i');
    onDot.className = 'red-key';
    const offDot = document.createElement('i');
    offDot.className = 'blue-key';
    polarity.append(onDot, `ON ${number.format(bin.on_count)}`, offDot, `OFF ${number.format(bin.off_count)}`);
    button.append(header, image, interval, polarity);
    button.addEventListener('click', () => {
      state.binIndex = bin.index;
      renderBinSelection();
    });
    grid.append(button);
  }
  renderBinSelection();
}

async function loadBins() {
  if (!state.session || state.viewMode !== 'bins') return;
  const key = binKey();
  const request = ++state.binRequest;
  $('binStatus').textContent = '원본 events.bin에서 기준 시각 이전 100 ms를 읽는 중…';
  $('binAlignment').textContent = '기준 시각을 계산하는 중…';
  $('binGrid').replaceChildren();
  $('eventImage').hidden = true;
  $('binPrevButton').disabled = true;
  $('binNextButton').disabled = true;
  $('eventModeLabel').textContent = '30 bins';
  $('eventFoot').textContent = '원본 이벤트 분석 중…';
  try {
    const data = state.binCache.get(key) || await getJSON(`/api/bins/${sessionPath(state.session.id)}/${state.selected}`);
    if (request !== state.binRequest || state.viewMode !== 'bins' || key !== binKey()) return;
    if (!state.binCache.has(key)) {
      state.binCache.set(key, data);
      if (state.binCache.size > 8) state.binCache.delete(state.binCache.keys().next().value);
    }
    state.binData = data;
    state.binKey = key;
    renderBinGrid();
  } catch (error) {
    if (request !== state.binRequest || state.viewMode !== 'bins') return;
    $('binStatus').textContent = '30개 bin을 불러오지 못했습니다.';
    toast('원본 이벤트를 불러오지 못했습니다. 서버 상태를 확인해 주세요.');
  }
}

function setViewMode(viewMode) {
  if (state.viewMode === viewMode) return;
  state.viewMode = viewMode;
  state.binRequest++;
  renderSelected();
  renderGallery();
}

function setBinsCollapsed(collapsed) {
  state.binsCollapsed = collapsed;
  $('binGridWrap').hidden = collapsed;
  $('binCollapseButton').textContent = collapsed ? '펼치기 ↓' : '접기 ↑';
  $('binCollapseButton').setAttribute('aria-expanded', String(!collapsed));
}

function moveBin(direction) {
  if (state.viewMode !== 'bins' || !state.binData || state.binKey !== binKey()) return;
  const next = Math.max(0, Math.min(29, state.binIndex + direction));
  if (next === state.binIndex) return;
  state.binIndex = next;
  renderBinSelection();
}

function renderSidebar() {
  $('sessionTotal').textContent = state.sessions.length;
  $('librarySummary').textContent = `${number.format(state.sessions.reduce((sum, s) => sum + s.frames, 0))}개 프레임 쌍`;
  const list = $('sessionList');
  list.replaceChildren();
  for (const session of state.sessions) {
    const button = document.createElement('button');
    button.className = `session-item${state.session?.id === session.id ? ' active' : ''}`;
    button.type = 'button';
    button.title = session.id;
    button.setAttribute('aria-current', state.session?.id === session.id ? 'true' : 'false');
    const icon = document.createElement('span');
    icon.className = 'session-symbol';
    icon.textContent = '▦';
    const text = document.createElement('span');
    text.className = 'session-item-text';
    const name = document.createElement('span');
    name.className = 'session-name';
    name.textContent = sessionLabel(session.id);
    const detail = document.createElement('span');
    detail.className = 'session-detail';
    detail.textContent = session.id.replace(/_\d{8}_\d{6}$/, '') || '세션';
    text.append(name, detail);
    const count = document.createElement('span');
    count.className = 'session-count';
    count.textContent = number.format(session.frames);
    button.append(icon, text, count);
    button.addEventListener('click', () => loadSession(session.id, 0));
    list.append(button);
  }
}

function renderSelected() {
  if (!state.session || !state.rows.length) return;
  const row = state.rows[state.selected];
  if (!row) return;
  $('currentIndex').textContent = `프레임 ${number.format(row.index + 1)} / ${number.format(state.rows.length)}장`;
  $('currentTime').textContent = timestamp(row.timestamp_us);
  $('frameNumber').textContent = `#${String(row.index).padStart(6, '0')}`;
  $('frameImage').src = imagePath('frame', row.index);
  const bins = state.viewMode === 'bins';
  $('binPanel').hidden = !bins;
  $('binPrevButton').hidden = !bins;
  $('binNextButton').hidden = !bins;
  document.querySelector('.viewer').classList.toggle('bin-mode', bins);
  for (const [id, active] of [['singleView', !bins], ['binView', bins]]) {
    $(id).classList.toggle('active', active);
    $(id).setAttribute('aria-pressed', String(active));
  }
  if (bins) {
    $('eventPanelTitle').textContent = 'RAW EVENT BIN';
    if (state.binData && state.binKey === binKey()) renderBinSelection();
    else loadBins();
  } else {
    $('eventPanelTitle').textContent = 'EVENT VIS';
    $('eventImage').hidden = false;
    $('eventImage').alt = '선택한 프레임의 이벤트 시각화';
    if (state.mode === 'saved') {
      $('eventImage').src = imagePath('event', row.index);
      $('eventModeLabel').textContent = '기록 당시 이미지';
      $('eventFoot').textContent = '저장된 시각화';
    } else {
      $('eventImage').src = `/api/event/${sessionPath(state.session.id)}/${row.index}?ms=${state.ms}`;
      $('eventModeLabel').textContent = `${(state.ms / 1000).toFixed(3)}초 누적`;
      $('eventFoot').textContent = '원본 이벤트에서 재생성';
    }
  }
  const like = likeFor(row.index);
  $('likeButton').disabled = false;
  $('likeButton').setAttribute('aria-pressed', String(like.count > 0));
  $('likeLabel').textContent = like.count > 0 ? '좋아요 취소' : '좋아요';
  $('likeCount').textContent = number.format(like.count);
  $('frameRange').max = String(state.rows.length - 1);
  $('frameRange').value = String(row.index);
  $('rangeEnd').textContent = number.format(state.rows.length);
  $('frameJump').max = String(state.rows.length - 1);
  $('frameJump').value = String(row.index);
  const atStart = row.index === 0;
  const atEnd = row.index === state.rows.length - 1;
  $('prevButton').disabled = atStart;
  $('nextButton').disabled = atEnd;
  $('framePrevOverlay').disabled = atStart;
  $('frameNextOverlay').disabled = atEnd;
  loadLikeOwners();
  updateURL();
}

function renderGallery() {
  const rows = filteredRows();
  const pages = Math.max(1, Math.ceil(rows.length / PAGE_SIZE));
  state.page = Math.min(state.page, pages - 1);
  $('resultCount').textContent = number.format(rows.length);
  $('emptyState').hidden = rows.length !== 0;
  $('pageLabel').textContent = `${state.page + 1} / ${pages}`;
  $('pagePrev').disabled = state.page === 0;
  $('pageNext').disabled = state.page >= pages - 1;
  const grid = $('galleryGrid');
  grid.replaceChildren();
  for (const row of rows.slice(state.page * PAGE_SIZE, (state.page + 1) * PAGE_SIZE)) {
    const card = document.createElement('button');
    card.type = 'button';
    card.className = `pair-card${state.selected === row.index ? ' selected' : ''}`;
    card.setAttribute('aria-label', `프레임 ${row.index}, 좋아요 ${likeFor(row.index).count}개`);
    card.setAttribute('aria-current', String(state.selected === row.index));
    const images = document.createElement('div');
    images.className = `card-images${state.viewMode === 'bins' ? ' single' : ''}`;
    for (const kind of state.viewMode === 'bins' ? ['frame'] : ['frame', 'event']) {
      const image = document.createElement('img');
      image.src = imagePath(kind, row.index);
      image.alt = '';
      image.loading = 'lazy';
      images.append(image);
    }
    const info = document.createElement('div');
    info.className = 'card-info';
    const title = document.createElement('div');
    const strong = document.createElement('strong');
    strong.textContent = `#${String(row.index).padStart(6, '0')}`;
    const time = document.createElement('span');
    time.className = 'card-time';
    time.textContent = timestamp(row.timestamp_us);
    title.append(strong, time);
    const like = document.createElement('span');
    like.className = `card-like${likeFor(row.index).count > 0 ? ' is-liked' : ''}`;
    like.textContent = `♥ ${likeFor(row.index).count}`;
    info.append(title, like);
    card.append(images, info);
    card.addEventListener('click', () => selectFrame(row.index));
    grid.append(card);
  }
}

function selectFrame(index) {
  if (!state.rows[index]) return;
  state.selected = index;
  const position = filteredRows().findIndex((row) => row.index === index);
  if (position >= 0) state.page = Math.floor(position / PAGE_SIZE);
  renderSelected();
  renderGallery();
}

async function loadSession(id, frame) {
  const request = ++state.request;
  $('sessionTitle').textContent = '세션을 여는 중…';
  try {
    const data = await getJSON(`/api/session/${sessionPath(id)}`);
    if (request !== state.request) return;
    state.session = data.session;
    state.rows = data.rows;
    state.likes = data.likes;
    state.account = data.user;
    renderAccount();
    state.likerKey = null;
    state.binData = null;
    state.binKey = null;
    state.binRequest++;
    state.page = 0;
    state.selected = Math.max(0, Math.min(Number(frame) || 0, state.rows.length - 1));
    $('sessionTitle').textContent = sessionLabel(id);
    $('sessionMeta').textContent = `${id} · ${number.format(data.rows.length)}개 프레임 · ${data.session.duration_s}초`;
    renderSidebar();
    selectFrame(state.selected);
  } catch (error) {
    if (request !== state.request) return;
    $('sessionTitle').textContent = '세션을 열 수 없습니다';
    toast('녹화 데이터를 불러오지 못했습니다. 서버 상태를 확인해 주세요.');
  }
}

function rangeToMs(value) {
  return Math.round(10 * Math.exp(Math.log(300) * Number(value) / 1000));
}

function msToRange(ms) {
  return Math.round(Math.log(ms / 10) / Math.log(300) * 1000);
}

function setWindow(ms, preserveInput = false) {
  if (!Number.isFinite(ms)) return;
  state.ms = Math.max(10, Math.min(3000, Math.round(ms)));
  state.mode = 'dynamic';
  if (!preserveInput) $('windowInput').value = (state.ms / 1000).toFixed(3);
  $('windowRange').value = String(msToRange(state.ms));
  clearTimeout(eventTimer);
  eventTimer = setTimeout(renderSelected, 130);
}

function toggleLike() {
  if (!state.session) return;
  const sessionId = state.session.id;
  const index = state.selected;
  likeQueue = likeQueue.then(async () => {
    try {
      const result = await getJSON(`/api/like/${sessionPath(sessionId)}/${index}`, {
        method: 'POST', headers: {'Content-Type': 'application/json'}, body: '{}',
      });
      if (state.session?.id !== sessionId) return;
      state.likes[String(index)] = {count: result.count, mine: result.liked};
      if (state.likedOnly && state.selected === index && result.count === 0) {
        const next = filteredRows()[0];
        if (next) state.selected = next.index;
      }
      renderSelected();
      renderGallery();
      loadLikeOwners(true);
    } catch (error) {
      toast('좋아요를 저장하지 못했습니다. 다시 시도해 주세요.');
    }
  });
  return likeQueue;
}

function move(direction) {
  const next = state.selected + direction;
  if (next >= 0 && next < state.rows.length) selectFrame(next);
}

async function share() {
  if (!state.session) return;
  try {
    await navigator.clipboard.writeText(location.href);
    toast('현재 프레임 링크를 복사했습니다.');
  } catch (error) {
    toast('주소창의 링크를 복사해 주세요.');
  }
}

function bindEvents() {
  $('authButton').addEventListener('click', () => state.account ? logout() : openLogin());
  $('authCancel').addEventListener('click', () => $('authDialog').close());
  $('authForm').addEventListener('submit', submitLogin);
  $('singleView').addEventListener('click', () => setViewMode('single'));
  $('binView').addEventListener('click', () => setViewMode('bins'));
  $('binPrevButton').addEventListener('click', () => moveBin(-1));
  $('binNextButton').addEventListener('click', () => moveBin(1));
  $('binCollapseButton').addEventListener('click', () => setBinsCollapsed(!state.binsCollapsed));
  $('prevButton').addEventListener('click', () => move(-1));
  $('nextButton').addEventListener('click', () => move(1));
  $('framePrevOverlay').addEventListener('click', () => move(-1));
  $('frameNextOverlay').addEventListener('click', () => move(1));
  $('frameRange').addEventListener('input', (event) => selectFrame(Number(event.target.value)));
  $('frameJump').addEventListener('change', (event) => selectFrame(Number(event.target.value)));
  $('windowRange').addEventListener('input', (event) => setWindow(rangeToMs(event.target.value)));
  $('windowInput').addEventListener('input', (event) => {
    const seconds = Number(event.target.value);
    if (seconds >= 0.01 && seconds <= 3) setWindow(seconds * 1000, true);
  });
  $('windowInput').addEventListener('change', (event) => setWindow(Number(event.target.value) * 1000));
  $('resetEvent').addEventListener('click', () => {
    state.mode = 'saved'; state.ms = 33;
    $('windowInput').value = '0.033';
    $('windowRange').value = String(msToRange(33));
    renderSelected();
  });
  $('likedOnly').addEventListener('change', (event) => {
    state.likedOnly = event.target.checked; state.page = 0;
    const visible = filteredRows();
    if (visible.length && !visible.some((row) => row.index === state.selected)) state.selected = visible[0].index;
    renderSelected(); renderGallery();
  });
  $('sortSelect').addEventListener('change', (event) => {
    state.sort = event.target.value; state.page = 0; renderSelected(); renderGallery();
  });
  $('pagePrev').addEventListener('click', () => {state.page--; renderGallery();});
  $('pageNext').addEventListener('click', () => {state.page++; renderGallery();});
  $('likeButton').addEventListener('click', toggleLike);
  $('shareButton').addEventListener('click', share);
  $('eventImage').addEventListener('error', () => toast('이벤트 이미지를 불러오지 못했습니다.'));
  window.addEventListener('keydown', (event) => {
    if ($('authDialog').open || (['INPUT', 'SELECT', 'TEXTAREA'].includes(document.activeElement.tagName) && document.activeElement.id !== 'frameJump')) return;
    if (state.viewMode === 'bins' && (event.key === 'ArrowUp' || event.key === 'ArrowDown' ||
        (event.shiftKey && (event.key === 'ArrowLeft' || event.key === 'ArrowRight')))) {
      event.preventDefault();
      moveBin(event.key === 'ArrowUp' || event.key === 'ArrowLeft' ? -1 : 1);
      return;
    }
    if (event.key === 'ArrowLeft') {event.preventDefault(); move(-1);}
    if (event.key === 'ArrowRight') {event.preventDefault(); move(1);}
    if (event.key.toLowerCase() === 'l') {event.preventDefault(); toggleLike();}
  });
}

async function init() {
  bindEvents();
  $('windowRange').value = String(msToRange(33));
  const params = new URLSearchParams(location.search);
  state.viewMode = params.get('view') === 'bins' ? 'bins' : 'single';
  const initialBin = Number(params.get('bin'));
  if (params.has('bin') && Number.isInteger(initialBin) && initialBin >= 0 && initialBin < 30) state.binIndex = initialBin;
  const seconds = Number(params.get('seconds'));
  if (params.has('seconds') && Number.isFinite(seconds) && seconds >= 0.01 && seconds <= 3) {
    state.ms = Math.round(seconds * 1000); state.mode = 'dynamic';
    $('windowInput').value = (state.ms / 1000).toFixed(3);
    $('windowRange').value = String(msToRange(state.ms));
  }
  try {
    const auth = await getJSON('/api/auth/me');
    state.account = auth.user;
    renderAccount();
    state.sessions = await getJSON('/api/sessions');
    state.sessions.sort((a, b) => {
      const aDate = a.id.match(/\d{8}_\d{6}$/)?.[0] || '';
      const bDate = b.id.match(/\d{8}_\d{6}$/)?.[0] || '';
      return bDate.localeCompare(aDate);
    });
    renderSidebar();
    if (!state.sessions.length) {
      $('sessionTitle').textContent = '녹화 세션이 없습니다';
      $('sessionMeta').textContent = '녹화 폴더 경로를 확인해 주세요.';
      return;
    }
    const wanted = params.get('session');
    const initial = state.sessions.find((session) => session.id === wanted)?.id || state.sessions[0].id;
    await loadSession(initial, params.get('frame'));
  } catch (error) {
    $('sessionTitle').textContent = '서버에 연결할 수 없습니다';
    toast('서버 상태를 확인해 주세요.');
  }
}

init();
