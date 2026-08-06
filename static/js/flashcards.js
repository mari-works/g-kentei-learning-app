function $(sel) { return document.querySelector(sel); }

let cards = FLASHCARDS || [];
let index = Number.isInteger(START_INDEX) ? START_INDEX : Number(START_INDEX || 0);
let flipped = false;
let randomMode = START_RANDOM === true || START_RANDOM === 'true';
let cardDirection = typeof CARD_DIRECTION === 'string' ? CARD_DIRECTION : 'term_to_meaning';
let restoredProgress = false;
let sessionResults = [];

const flashcard = $('#flashcard');
const cardInner = $('#card-inner');
const cardCategory = $('#card-category');
const cardDescription = $('#card-description');
const cardTerm = $('#card-term');
const cardCount = $('#card-count');
const cardProgress = $('#card-progress');
const nextBtn = $('#next-btn');
const prevBtn = $('#prev-btn');
const pauseBtn = $('#pause-btn');
const randomToggle = $('#random-toggle');
const sessionCount = $('#session-count');
const sessionMastered = $('#session-mastered');
const sessionWeak = $('#session-weak');
const statusBtns = document.querySelectorAll('.status-btn');
const directionBtns = document.querySelectorAll('.direction-btn');
const idleMessage = $('#idle-message');
let studiedInSession = 0;
let lastActivityAt = Date.now();
let pendingActiveSeconds = 0;
let totalActiveSeconds = 0;

function markActivity() {
  const now = Date.now();
  const deltaSeconds = Math.max(0, Math.floor((now - lastActivityAt) / 1000));
  if (deltaSeconds > 0 && deltaSeconds < 300) {
    pendingActiveSeconds += deltaSeconds;
    totalActiveSeconds += deltaSeconds;
    if (idleMessage) idleMessage.classList.add('hidden');
  } else if (deltaSeconds >= 300) {
    if (idleMessage) idleMessage.classList.remove('hidden');
  }
  lastActivityAt = now;
}

function consumePendingActiveSeconds() {
  const seconds = pendingActiveSeconds;
  pendingActiveSeconds = 0;
  return seconds;
}

function resetFlip() {
  flipped = false;
  if (cardInner) {
    cardInner.style.transition = 'none';
    cardInner.style.transform = '';
    cardInner.offsetHeight;
    cardInner.style.transition = '';
  }
}

function shuffle(a) {
  for (let i = a.length - 1; i > 0; i--) {
    const j = Math.floor(Math.random() * (i + 1));
    [a[i], a[j]] = [a[j], a[i]];
  }
}

function renderCard(i) {
  if (!cards || cards.length === 0) {
    cardCategory.textContent = '';
    cardDescription.textContent = '表示できるカードがありません。';
    cardTerm.textContent = '';
    cardCount.textContent = '0';
    if (cardProgress) cardProgress.style.width = '0%';
    return;
  }
  index = ((i % cards.length) + cards.length) % cards.length;
  const c = cards[i % cards.length];
  cardCategory.textContent = c.category || c.big_category || '未設定';
  if (cardDirection === 'term_to_meaning') {
    cardDescription.textContent = c.keyword || c.item_name || '';
    cardTerm.textContent = c.meaning || '';
  } else {
    cardDescription.textContent = c.meaning || '';
    cardTerm.textContent = c.keyword || c.item_name || '';
  }
  cardCount.textContent = (i % cards.length) + 1;
  if (cardProgress) {
    cardProgress.style.width = `${Math.round(((i % cards.length) + 1) / cards.length * 100)}%`;
  }
  directionBtns.forEach(btn => {
    btn.classList.toggle('is-active', btn.getAttribute('data-direction') === cardDirection);
  });
  saveProgress();
}

function saveProgress() {
  if (!cards || cards.length === 0) return;
  const currentUrl = new URL(window.location.href);
  currentUrl.searchParams.delete('resume_index');
  localStorage.setItem('flashcardProgress', JSON.stringify({
    url: currentUrl.pathname + currentUrl.search,
    index: index,
    total: cards.length,
    direction: cardDirection,
    cards: cards,
    savedAt: new Date().toISOString()
  }));
}

function getResultCounts() {
  return sessionResults.reduce((acc, item) => {
    if (item.status === '理解済み') acc.mastered += 1;
    if (item.status === 'あいまい') acc.ambiguous += 1;
    if (item.status === '未理解') acc.weak += 1;
    return acc;
  }, { mastered: 0, ambiguous: 0, weak: 0 });
}

function finishSession() {
  markActivity();
  const counts = getResultCounts();
  const remainingActiveSeconds = consumePendingActiveSeconds();
  if (remainingActiveSeconds > 0) {
    const payloadData = {
      session_type: 'term',
      category: typeof FLASHCARD_RANGE_LABEL === 'string' ? FLASHCARD_RANGE_LABEL : null,
      learning_mode: typeof FLASHCARD_LEARNING_MODE === 'string' ? FLASHCARD_LEARNING_MODE : null,
      learning_category: typeof FLASHCARD_LEARNING_CATEGORY === 'string' ? FLASHCARD_LEARNING_CATEGORY : null,
      duration_seconds: remainingActiveSeconds
    };
    if (navigator.sendBeacon) {
      const payload = new Blob([JSON.stringify(payloadData)], { type: 'application/json' });
      navigator.sendBeacon('/api/record_study_time', payload);
    } else {
      fetch('/api/record_study_time', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payloadData),
        keepalive: true
      }).catch(() => {});
    }
  }
  sessionStorage.setItem('flashcardResult', JSON.stringify({
    total: cards.length,
    answered: sessionResults.length,
    unanswered: Math.max(0, cards.length - sessionResults.length),
    mastered: counts.mastered,
    ambiguous: counts.ambiguous,
    weak: counts.weak,
    activeSeconds: totalActiveSeconds,
    rangeLabel: typeof FLASHCARD_RANGE_LABEL === 'string' ? FLASHCARD_RANGE_LABEL : '今回の学習'
  }));
  localStorage.removeItem('flashcardProgress');
  window.location.href = '/flashcards/result';
}

function flipCard() {
  markActivity();
  flipped = !flipped;
  if (flipped) {
    cardInner.style.transform = 'rotateY(180deg)';
  } else {
    cardInner.style.transform = '';
  }
}

function gotoNext() {
  if (!cards || cards.length === 0) return;
  markActivity();
  if (index >= cards.length - 1) {
    finishSession();
    return;
  }
  resetFlip();
  index = index + 1;
  renderCard(index);
}

function gotoPrev() {
  if (!cards || cards.length === 0) return;
  markActivity();
  resetFlip();
  index = (index - 1 + cards.length) % cards.length;
  renderCard(index);
}

function postStatus(id, status, durationSeconds) {
  fetch('/api/set_status', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      id: id,
      status: status,
      duration_seconds: durationSeconds || 0,
      learning_mode: typeof FLASHCARD_LEARNING_MODE === 'string' ? FLASHCARD_LEARNING_MODE : null,
      learning_category: typeof FLASHCARD_LEARNING_CATEGORY === 'string' ? FLASHCARD_LEARNING_CATEGORY : null,
      filter_status: typeof FLASHCARD_FILTER_STATUS === 'string' ? FLASHCARD_FILTER_STATUS : null
    }),
  }).then(resp => resp.json()).then(data => {
    console.log('status saved', data);
  }).catch(err => console.error(err));
}

function answerCurrent(status) {
  if (!cards || cards.length === 0) return;
  markActivity();
  const current = cards[index % cards.length];
  postStatus(current.id, status, consumePendingActiveSeconds());
  sessionResults = sessionResults.filter(item => item.id !== current.id);
  sessionResults.push({ id: current.id, status: status });
  studiedInSession += 1;
  if (sessionCount) sessionCount.textContent = studiedInSession;
  if (status === '理解済み' && sessionMastered) {
    sessionMastered.textContent = Number(sessionMastered.textContent || 0) + 1;
  }
  if (status === '未理解' && sessionWeak) {
    sessionWeak.textContent = Number(sessionWeak.textContent || 0) + 1;
  }
  if (index >= cards.length - 1) {
    finishSession();
  } else {
    gotoNext();
  }
}

if (flashcard) {
  flashcard.addEventListener('click', (e) => {
    if (e.target.closest('.status-btn')) return;
    if (flipped) {
      gotoNext();
    } else {
      flipCard();
    }
  });
}

if (nextBtn) {
  nextBtn.addEventListener('click', () => {
    gotoNext();
  });
}

if (prevBtn) {
  prevBtn.addEventListener('click', () => {
    gotoPrev();
  });
}

if (pauseBtn) {
  pauseBtn.addEventListener('click', () => {
    window.location.href = '/flashcards/settings';
  });
}

if (randomToggle) {
  randomToggle.addEventListener('change', (e) => {
    markActivity();
    randomMode = e.target.checked;
    if (randomMode) shuffle(cards);
    resetFlip();
    index = 0;
    renderCard(index);
  });
}

statusBtns.forEach(btn => {
  btn.addEventListener('click', (e) => {
    const status = btn.getAttribute('data-status');
    answerCurrent(status);
  });
});

directionBtns.forEach(btn => {
  btn.addEventListener('click', () => {
    markActivity();
    cardDirection = btn.getAttribute('data-direction') || 'term_to_meaning';
    resetFlip();
    renderCard(index);
  });
});

document.addEventListener('keydown', (event) => {
  const target = event.target;
  if (target && ['INPUT', 'SELECT', 'TEXTAREA'].includes(target.tagName)) return;

  if (event.key === ' ' || event.key === 'Enter') {
    event.preventDefault();
    if (flipped) {
      gotoNext();
    } else {
      flipCard();
    }
    return;
  }

  if (event.key === 'ArrowRight') {
    event.preventDefault();
    gotoNext();
    return;
  }

  if (event.key === 'ArrowLeft') {
    event.preventDefault();
    gotoPrev();
    return;
  }

  const statusByKey = {
    '1': '理解済み',
    '2': 'あいまい',
    '3': '未理解'
  };
  if (statusByKey[event.key]) {
    event.preventDefault();
    answerCurrent(statusByKey[event.key]);
  }
});

// initial setup
try {
  const saved = JSON.parse(localStorage.getItem('flashcardProgress') || 'null');
  const currentUrl = new URL(window.location.href);
  const resumeIndex = currentUrl.searchParams.get('resume_index');
  currentUrl.searchParams.delete('resume_index');
  if (resumeIndex !== null && saved && saved.url === currentUrl.pathname + currentUrl.search && Array.isArray(saved.cards)) {
    cards = saved.cards;
    restoredProgress = true;
  }
} catch (error) {
  localStorage.removeItem('flashcardProgress');
}

if (randomMode && !restoredProgress) shuffle(cards);
if (cards.length > 0) {
  index = Math.max(0, Math.min(index, cards.length - 1));
}
renderCard(index);
