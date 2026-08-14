// First-run product tour: a short, skippable walkthrough shown once after
// the first successful Telegram login, replayable later from Settings.
// Modeled on the same wizard-card visual pattern as the setup flow
// (#setup-overlay) for consistency, rather than inventing a second style of
// full-screen overlay.
//
// Relies on API/escapeHTML from app.js -- safe regardless of <script> load
// order since those are only referenced from inside callbacks that run
// after every script has finished parsing.

const TOUR_STEPS = [
  {
    title: 'Welcome to BackitSnappy',
    body: 'Your personal Telegram account becomes private, effectively unlimited cloud storage for photos, videos, and files -- organized however you like.',
  },
  {
    title: 'Albums',
    body: 'Everything lives in Albums -- Finder-style folders, each backed by its own private Telegram channel. Create one, drag files in, and download or delete anytime.',
  },
  {
    title: 'Settings -- your Telegram account',
    body: 'Your Telegram session and connection status live here. Your per-file upload limit (2GB or 4GB, based on your account) is detected automatically.',
  },
  {
    title: 'Settings -- iPhone access (Tailscale)',
    body: 'Turn this on to let your iPhone upload directly to BackitSnappy over your own private Tailscale network -- nothing is exposed to the public internet.',
  },
  {
    title: 'Settings -- iOS Shortcut setup',
    body: 'Once Tailscale access is on, this section shows the Upload URL, Pairing Token, and Album ID your iPhone Shortcut needs -- each with a Copy button, ready to paste in.',
  },
];

let TOUR_INDEX = 0;

function renderTourStep() {
  const step = TOUR_STEPS[TOUR_INDEX];
  document.getElementById('tour-title').textContent = step.title;
  document.getElementById('tour-body').textContent = step.body;
  document.getElementById('btn-tour-next').textContent =
    TOUR_INDEX === TOUR_STEPS.length - 1 ? 'Done' : 'Next';
  const dots = document.getElementById('tour-dots');
  dots.innerHTML = TOUR_STEPS.map((_, i) =>
    `<span class="tour-dot${i === TOUR_INDEX ? ' active' : ''}"></span>`).join('');
}

function openTour() {
  TOUR_INDEX = 0;
  renderTourStep();
  document.getElementById('tour-overlay').classList.remove('hidden');
}

async function closeTourAndMarkComplete() {
  document.getElementById('tour-overlay').classList.add('hidden');
  try {
    await API.request('/api/settings/onboarding_completed', { method: 'PUT', json: { completed: true } });
  } catch (e) { /* best-effort -- not worth blocking the UI over */ }
}

function initTour() {
  document.getElementById('btn-tour-skip').addEventListener('click', closeTourAndMarkComplete);
  document.getElementById('btn-tour-next').addEventListener('click', () => {
    if (TOUR_INDEX === TOUR_STEPS.length - 1) {
      closeTourAndMarkComplete();
      return;
    }
    TOUR_INDEX++;
    renderTourStep();
  });
  document.getElementById('btn-replay-tour').addEventListener('click', openTour);
}
